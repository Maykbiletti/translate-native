"use strict";

const fs = require("node:fs");
const path = require("node:path");
const {
  LanguageGuardBlocked,
  routeHostContext,
  verifyForDelivery,
} = require("./node-language-guard");

const SERVER_NAME = "blun-language-guard";
const MAX_SERVICE_TOKEN_BYTES = 64 * 1024;

function protectedFileIdentity(stats) {
  return {
    dev: stats.dev,
    ino: stats.ino,
    size: stats.size,
    ctimeMs: stats.ctimeMs,
    mtimeMs: stats.mtimeMs,
  };
}

function sameProtectedFile(stats, expected) {
  return stats.dev === expected.dev && stats.ino === expected.ino
    && stats.size === expected.size && stats.ctimeMs === expected.ctimeMs
    && stats.mtimeMs === expected.mtimeMs;
}

function validateServiceTokenStats(stats) {
  if (!stats.isFile() || stats.isSymbolicLink()) throw new Error("service token must be a regular file");
  if (stats.size < 32 || stats.size > MAX_SERVICE_TOKEN_BYTES) throw new Error("service token has an invalid size");
  if (process.platform !== "win32" && (stats.mode & 0o077) !== 0) throw new Error("service token permissions are too broad");
  if (typeof process.getuid === "function" && stats.uid !== process.getuid()) throw new Error("service token has the wrong owner");
}

function readProtectedServiceToken(destination) {
  const before = fs.lstatSync(destination);
  validateServiceTokenStats(before);
  const noFollow = typeof fs.constants.O_NOFOLLOW === "number" ? fs.constants.O_NOFOLLOW : 0;
  const descriptor = fs.openSync(destination, fs.constants.O_RDONLY | noFollow);
  try {
    const opened = fs.fstatSync(descriptor);
    validateServiceTokenStats(opened);
    if (!sameProtectedFile(opened, protectedFileIdentity(before))) throw new Error("service token changed while opening");
    const buffer = Buffer.alloc(MAX_SERVICE_TOKEN_BYTES + 1);
    let size = 0;
    while (size < buffer.length) {
      const count = fs.readSync(descriptor, buffer, size, buffer.length - size, null);
      if (count === 0) break;
      size += count;
    }
    const after = fs.fstatSync(descriptor);
    if (!sameProtectedFile(after, protectedFileIdentity(opened))) throw new Error("service token changed while reading");
    if (size > MAX_SERVICE_TOKEN_BYTES) throw new Error("service token has an invalid size");
    const token = new TextDecoder("utf-8", { fatal: true }).decode(buffer.subarray(0, size)).replace(/^\uFEFF/, "").trim();
    if (token.length < 32) throw new Error("service token is invalid");
    return token;
  } finally {
    fs.closeSync(descriptor);
  }
}

function loadLegacyGuardConfig(userHome) {
  const file = path.join(userHome, ".blun", "mcp.json");
  let parsed;
  try {
    parsed = JSON.parse(fs.readFileSync(file, "utf8").replace(/^\uFEFF/, ""));
  } catch {
    return null;
  }
  const server = parsed?.mcpServers?.[SERVER_NAME];
  if (!server || typeof server !== "object") return null;
  const env = server.env && typeof server.env === "object" ? server.env : {};
  return {
    name: SERVER_NAME,
    command: String(server.command || ""),
    args: Array.isArray(server.args) ? server.args.map(String) : [],
    cwd: String(server.cwd || ""),
    endpoint: String(env.BLUN_LANGUAGE_GUARD_SERVICE_ENDPOINT || ""),
    tokenFile: String(env.BLUN_LANGUAGE_GUARD_SERVICE_TOKEN_FILE || ""),
  };
}

function bootstrapLanguageGuardMcp({ userHome, store }) {
  const current = store.getAllInternal().find(item => String(item.name || "").toLowerCase() === SERVER_NAME);
  if (current) return { installed: false, id: current.id, reason: "already-installed" };
  const legacy = loadLegacyGuardConfig(userHome);
  if (!legacy?.command || !legacy.endpoint || !legacy.tokenFile) {
    return { installed: false, reason: "legacy-config-missing" };
  }
  const saved = store.save({
    name: SERVER_NAME,
    transport: "stdio",
    command: legacy.command,
    args: legacy.args,
    cwd: legacy.cwd,
    enabled: true,
    secretText: [
      `BLUN_LANGUAGE_GUARD_SERVICE_ENDPOINT=${legacy.endpoint}`,
      `BLUN_LANGUAGE_GUARD_SERVICE_TOKEN_FILE=${legacy.tokenFile}`,
    ].join("\n"),
  });
  if (!saved?.ok || !saved.savedId) throw new LanguageGuardBlocked("language guard MCP bootstrap failed", "guard_unavailable");
  return { installed: true, id: saved.savedId };
}

function resolveGuardConnection({ store, environment = process.env }) {
  const server = store.getAllInternal().find(item => String(item.name || "").toLowerCase() === SERVER_NAME);
  let secrets = {};
  if (server) {
    try { secrets = store.getSecrets(server.id) || {}; }
    catch { secrets = {}; }
  }
  const endpoint = String(
    environment.BLUN_LANGUAGE_GUARD_SERVICE_ENDPOINT
    || secrets.BLUN_LANGUAGE_GUARD_SERVICE_ENDPOINT
    || "",
  ).trim();
  const tokenFile = String(
    environment.BLUN_LANGUAGE_GUARD_SERVICE_TOKEN_FILE
    || secrets.BLUN_LANGUAGE_GUARD_SERVICE_TOKEN_FILE
    || "",
  ).trim();
  if (!server || server.enabled === false || !endpoint || !tokenFile) {
    throw new LanguageGuardBlocked("mandatory language guard is not configured", "guard_unavailable");
  }
  let serviceToken;
  try { serviceToken = readProtectedServiceToken(tokenFile); }
  catch { throw new LanguageGuardBlocked("mandatory language guard token is unavailable", "guard_unavailable"); }
  if (serviceToken.length < 32) {
    throw new LanguageGuardBlocked("mandatory language guard token is invalid", "guard_unavailable");
  }
  return { endpoint, serviceToken };
}

function resolveLanguage(meta, config) {
  const candidates = [
    [meta?.languageGuardLanguage, "meta.languageGuardLanguage"],
    [config?.languageGuardLanguage, "config.languageGuardLanguage"],
    [config?.responseLanguage, "config.responseLanguage"],
    [config?.language, "config.language"],
    [meta?.telegram?.conversationLanguage, "meta.telegram.conversationLanguage"],
    // Telegram's senderLanguageCode describes the sender's client/interface
    // language. It is not reliable evidence for the language of this message,
    // so retain it only as a backwards-compatible last resort.
    [meta?.telegram?.senderLanguageCode, "meta.telegram.senderLanguageCode"],
  ];
  for (const [value, source] of candidates) {
    const language = String(value || "").trim();
    if (language) return { language, source };
  }
  return { language: "", source: "missing" };
}

function languageFromMeta(meta, config) {
  return resolveLanguage(meta, config).language;
}

function createBlunLanguageGuard({ store, getConfig, environment = process.env }) {
  function context({ messages, meta = {}, channel = "desktop" }) {
    const prompt = String(messages?.[messages.length - 1]?.content || "");
    const taskKind = String(meta.languageGuardTaskKind || "response").trim().toLowerCase();
    const sourceText = taskKind === "translation"
      ? String(meta.languageGuardSourceText || "")
      : "";
    const languageResolution = resolveLanguage(meta, getConfig?.() || {});
    const language = languageResolution.language;
    const route = routeHostContext({
      task_kind: taskKind,
      operation: taskKind === "translation" ? "translation" : "chat",
      source_text: sourceText,
      target_language: taskKind === "translation" ? language : undefined,
      response_language: taskKind === "response" ? language : undefined,
      content_type: meta.languageGuardContentType || "prose",
    });
    const connection = resolveGuardConnection({ store, environment });
    return {
      hostContext: {
        task_kind: route.taskKind,
        operation: route.taskKind === "translation" ? "translation" : "chat",
        source_text: route.sourceText,
        target_language: route.taskKind === "translation" ? route.language : undefined,
        response_language: route.taskKind === "response" ? route.language : undefined,
        content_type: route.contentType,
      },
      endpoint: connection.endpoint,
      serviceToken: connection.serviceToken,
      agentId: String(meta.agentName || meta.agentId || "blun-code"),
      channel: String(channel || "desktop"),
      prompt,
      route,
      languageSource: languageResolution.source,
    };
  }

  function mandatoryInstruction(guardContext) {
    const tool = guardContext.route.taskKind === "translation" ? "release_translation" : "release_response";
    const translationRule = guardContext.route.taskKind === "translation"
      ? "Load and apply the installed translate-native skill/plugin before drafting. Use the complete trusted source supplied by the host."
      : "If the request is actually a translation but the host did not mark it as translation, do not translate; explain that trusted translation routing is required.";
    return [
      "[BLUN LANGUAGE GUARD — MANDATORY]",
      `The host classified this output as ${guardContext.route.taskKind} in ${guardContext.route.language}.`,
      `Pass language exactly as ${JSON.stringify(guardContext.route.language)} to ${tool}; do not substitute a base language or another locale.`,
      translationRule,
      `Before final output, call ${tool} for the complete final candidate with truthful attestations.`,
      "Final output must be exactly one JSON object with only target_text and release_token.",
      "Do not stream, print, or send the candidate through another channel.",
      "[/BLUN LANGUAGE GUARD]",
    ].join("\n");
  }

  function decorateMessages(messages, guardContext) {
    const copy = Array.isArray(messages) ? messages.map(item => ({ ...item })) : [];
    if (!copy.length) return copy;
    const index = copy.length - 1;
    copy[index].content = `${String(copy[index].content || "")}\n\n${mandatoryInstruction(guardContext)}`;
    return copy;
  }

  function bufferedEmitter(emit) {
    if (typeof emit !== "function") return undefined;
    return event => {
      if (event?.type === "text-delta" || event?.type === "done") return;
      emit(event);
    };
  }

  async function releaseResult(result, guardContext, emit) {
    if (result?.error || result?.cancelled) return result;
    const rawEnvelope = result?.answer || result?.reply || "";
    const verified = await verifyForDelivery({
      rawEnvelope,
      hostContext: guardContext.hostContext,
      endpoint: guardContext.endpoint,
      serviceToken: guardContext.serviceToken,
      agentId: guardContext.agentId,
      channel: guardContext.channel,
    });
    if (typeof emit === "function") {
      emit({ type: "text-delta", delta: verified.text, languageGuardVerified: true });
      emit({ type: "done", answer: verified.text, languageGuardVerified: true });
    }
    return {
      ...result,
      answer: verified.text,
      reply: verified.text,
      languageGuardVerified: true,
      languageGuard: {
        taskKind: verified.route.taskKind,
        language: verified.route.language,
        languageSource: guardContext.languageSource,
        version: verified.verification?.version || "",
      },
    };
  }

  return {
    mandatory: true,
    context,
    mandatoryInstruction,
    decorateMessages,
    bufferedEmitter,
    releaseResult,
  };
}

module.exports = {
  SERVER_NAME,
  loadLegacyGuardConfig,
  readProtectedServiceToken,
  bootstrapLanguageGuardMcp,
  resolveGuardConnection,
  resolveLanguage,
  languageFromMeta,
  createBlunLanguageGuard,
};
