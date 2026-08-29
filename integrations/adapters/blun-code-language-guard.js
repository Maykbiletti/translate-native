"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const {
  LanguageGuardBlocked,
  routeHostContext,
  verifyForDelivery,
} = require("./node-language-guard");

const SERVER_NAME = "blun-language-guard";
const MAX_SERVICE_TOKEN_BYTES = 64 * 1024;
const MAX_BLUN_MCP_CONFIG_BYTES = 1024 * 1024;

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
  if (stats.nlink !== 1) throw new Error("service token must not have additional hard links");
  if (stats.size < 32 || stats.size > MAX_SERVICE_TOKEN_BYTES) throw new Error("service token has an invalid size");
  if (process.platform !== "win32" && (stats.mode & 0o077) !== 0) throw new Error("service token permissions are too broad");
  if (typeof process.getuid === "function" && stats.uid !== process.getuid()) throw new Error("service token has the wrong owner");
}

function protectedDirectoryIdentity(stats) {
  return {
    dev: stats.dev,
    ino: stats.ino,
    mode: stats.mode,
    uid: stats.uid,
    gid: stats.gid,
  };
}

function sameProtectedDirectory(stats, expected) {
  return stats.dev === expected.dev && stats.ino === expected.ino
    && stats.mode === expected.mode && stats.uid === expected.uid
    && stats.gid === expected.gid;
}

function validateProtectedDirectoryStats(stats, directory, label) {
  if (!stats.isDirectory() || stats.isSymbolicLink()) {
    throw new Error(`${label} directory must be a directory: ${directory}`);
  }
  if (process.platform !== "win32" && (stats.mode & 0o022) !== 0) {
    throw new Error(`${label} directory is writable outside its owner: ${directory}`);
  }
  if (typeof process.getuid === "function" && stats.uid !== process.getuid()) {
    throw new Error(`${label} directory has the wrong owner: ${directory}`);
  }
}

function existingProtectedDirectoryAnchor(directory, label) {
  let candidate = directory;
  while (true) {
    try {
      fs.lstatSync(candidate);
      return candidate;
    } catch (error) {
      if (!error || error.code !== "ENOENT") throw error;
      const parent = path.dirname(candidate);
      if (parent === candidate) {
        throw new Error(`${label} directory has no existing anchor: ${directory}`);
      }
      candidate = parent;
    }
  }
}

function openProtectedDirectory(destination, label, { allowMissing = false } = {}) {
  const absoluteFile = path.resolve(destination);
  const directory = path.dirname(absoluteFile);
  if (process.platform === "win32") {
    let details;
    try {
      details = fs.lstatSync(directory);
    } catch (error) {
      if (allowMissing && error && error.code === "ENOENT") return null;
      throw error;
    }
    validateProtectedDirectoryStats(details, directory, label);
    return {
      accessPath: absoluteFile,
      descriptor: null,
      directory,
      identity: protectedDirectoryIdentity(details),
    };
  }
  const home = path.resolve(os.homedir());
  const underHome = directory === home || directory.startsWith(`${home}${path.sep}`);
  const anchor = underHome ? home : existingProtectedDirectoryAnchor(directory, label);
  const relative = path.relative(anchor, directory);
  const components = relative ? relative.split(path.sep) : [];
  const noFollow = typeof fs.constants.O_NOFOLLOW === "number" ? fs.constants.O_NOFOLLOW : 0;
  const directoryOnly = typeof fs.constants.O_DIRECTORY === "number" ? fs.constants.O_DIRECTORY : 0;
  const flags = fs.constants.O_RDONLY | noFollow | directoryOnly;
  const descriptorRoot = process.platform === "linux" ? "/proc/self/fd" : "/dev/fd";
  let descriptor = null;
  let current = anchor;
  try {
    descriptor = fs.openSync(anchor, flags);
    validateProtectedDirectoryStats(fs.fstatSync(descriptor), anchor, label);
    for (const component of components) {
      current = path.join(current, component);
      let child;
      try {
        child = fs.openSync(path.join(descriptorRoot, String(descriptor), component), flags);
      } catch (error) {
        if (allowMissing && error && error.code === "ENOENT") {
          fs.closeSync(descriptor);
          descriptor = null;
          return null;
        }
        throw error;
      }
      try {
        validateProtectedDirectoryStats(fs.fstatSync(child), current, label);
      } catch (error) {
        fs.closeSync(child);
        throw error;
      }
      fs.closeSync(descriptor);
      descriptor = child;
    }
    const identity = protectedDirectoryIdentity(fs.fstatSync(descriptor));
    const accessPath = path.join(descriptorRoot, String(descriptor), path.basename(absoluteFile));
    return { accessPath, descriptor, directory, identity };
  } catch (error) {
    if (descriptor !== null) fs.closeSync(descriptor);
    if (error && String(error.message || "").startsWith(`${label} directory`)) throw error;
    throw new Error(`${label} directory cannot be opened safely: ${current}`);
  }
}

function closeProtectedDirectory(protectedDirectory, label) {
  if (protectedDirectory.descriptor === null) {
    const current = fs.lstatSync(protectedDirectory.directory);
    validateProtectedDirectoryStats(current, protectedDirectory.directory, label);
    if (!sameProtectedDirectory(current, protectedDirectory.identity)) {
      throw new Error(`${label} directory changed while reading: ${protectedDirectory.directory}`);
    }
    return;
  }
  try {
    const held = fs.fstatSync(protectedDirectory.descriptor);
    const current = fs.lstatSync(protectedDirectory.directory);
    validateProtectedDirectoryStats(current, protectedDirectory.directory, label);
    if (!sameProtectedDirectory(held, protectedDirectory.identity)
        || !sameProtectedDirectory(current, protectedDirectory.identity)) {
      throw new Error(`${label} directory changed while reading: ${protectedDirectory.directory}`);
    }
  } finally {
    fs.closeSync(protectedDirectory.descriptor);
  }
}

function readProtectedServiceToken(destination) {
  const protectedDirectory = openProtectedDirectory(destination, "service token");
  const tokenFile = protectedDirectory.accessPath;
  try {
    const before = fs.lstatSync(tokenFile);
    validateServiceTokenStats(before);
    const noFollow = typeof fs.constants.O_NOFOLLOW === "number" ? fs.constants.O_NOFOLLOW : 0;
    const descriptor = fs.openSync(tokenFile, fs.constants.O_RDONLY | noFollow);
    try {
      const opened = fs.fstatSync(descriptor);
      validateServiceTokenStats(opened);
      if (!sameProtectedFile(opened, protectedFileIdentity(before)) || opened.nlink !== before.nlink) {
        throw new Error("service token changed while opening");
      }
      const buffer = Buffer.alloc(MAX_SERVICE_TOKEN_BYTES + 1);
      let size = 0;
      while (size < buffer.length) {
        const count = fs.readSync(descriptor, buffer, size, buffer.length - size, null);
        if (count === 0) break;
        size += count;
      }
      const after = fs.fstatSync(descriptor);
      const afterPath = fs.lstatSync(tokenFile);
      if (!sameProtectedFile(after, protectedFileIdentity(opened)) || after.nlink !== opened.nlink
          || !sameProtectedFile(afterPath, protectedFileIdentity(opened)) || afterPath.nlink !== opened.nlink) {
        throw new Error("service token changed while reading");
      }
      if (size > MAX_SERVICE_TOKEN_BYTES) throw new Error("service token has an invalid size");
      const token = new TextDecoder("utf-8", { fatal: true }).decode(buffer.subarray(0, size)).replace(/^\uFEFF/, "").trim();
      if (token.length < 32) throw new Error("service token is invalid");
      return token;
    } finally {
      fs.closeSync(descriptor);
    }
  } finally {
    closeProtectedDirectory(protectedDirectory, "service token");
  }
}

function validateLegacyConfigStats(stats) {
  if (!stats.isFile() || stats.isSymbolicLink()) throw new Error("BLUN MCP configuration must be a regular file");
  if (stats.nlink !== 1) throw new Error("BLUN MCP configuration must not have additional hard links");
  if (stats.size > MAX_BLUN_MCP_CONFIG_BYTES) throw new Error("BLUN MCP configuration exceeds the size limit");
  if (process.platform !== "win32" && (stats.mode & 0o022) !== 0) {
    throw new Error("BLUN MCP configuration is writable outside its owner");
  }
  if (typeof process.getuid === "function" && stats.uid !== process.getuid()) {
    throw new Error("BLUN MCP configuration has the wrong owner");
  }
}

function readProtectedLegacyConfig(destination) {
  const protectedDirectory = openProtectedDirectory(
    destination,
    "BLUN MCP configuration",
    { allowMissing: true },
  );
  if (protectedDirectory === null) return null;
  const configFile = protectedDirectory.accessPath;
  try {
    let before;
    try { before = fs.lstatSync(configFile); }
    catch (error) {
      if (error?.code === "ENOENT") return null;
      throw error;
    }
    validateLegacyConfigStats(before);
    const expected = protectedFileIdentity(before);
    const noFollow = typeof fs.constants.O_NOFOLLOW === "number" ? fs.constants.O_NOFOLLOW : 0;
    const descriptor = fs.openSync(configFile, fs.constants.O_RDONLY | noFollow);
    try {
      const opened = fs.fstatSync(descriptor);
      validateLegacyConfigStats(opened);
      if (!sameProtectedFile(opened, expected) || opened.nlink !== before.nlink) {
        throw new Error("BLUN MCP configuration changed while opening");
      }
      const buffer = Buffer.alloc(MAX_BLUN_MCP_CONFIG_BYTES + 1);
      let size = 0;
      while (size < buffer.length) {
        const count = fs.readSync(descriptor, buffer, size, buffer.length - size, null);
        if (count === 0) break;
        size += count;
      }
      const after = fs.fstatSync(descriptor);
      const afterPath = fs.lstatSync(configFile);
      if (
        !sameProtectedFile(after, expected) || after.nlink !== before.nlink
        || !sameProtectedFile(afterPath, expected) || afterPath.nlink !== before.nlink
      ) throw new Error("BLUN MCP configuration changed while reading");
      if (size > MAX_BLUN_MCP_CONFIG_BYTES) throw new Error("BLUN MCP configuration exceeds the size limit");
      return new TextDecoder("utf-8", { fatal: true })
        .decode(buffer.subarray(0, size)).replace(/^\uFEFF/, "");
    } finally {
      fs.closeSync(descriptor);
    }
  } finally {
    closeProtectedDirectory(protectedDirectory, "BLUN MCP configuration");
  }
}

function loadLegacyGuardConfig(userHome) {
  const file = path.join(userHome, ".blun", "mcp.json");
  const raw = readProtectedLegacyConfig(file);
  if (raw === null) return null;
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new Error("BLUN MCP configuration is not valid JSON");
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("BLUN MCP configuration root must be an object");
  }
  if (parsed.mcpServers !== undefined && (
    !parsed.mcpServers || typeof parsed.mcpServers !== "object" || Array.isArray(parsed.mcpServers)
  )) throw new Error("BLUN mcpServers must be an object");
  const server = parsed?.mcpServers?.[SERVER_NAME];
  if (server === undefined) return null;
  if (!server || typeof server !== "object" || Array.isArray(server)) {
    throw new Error("BLUN language-guard MCP entry must be an object");
  }
  if (server.env !== undefined && (
    !server.env || typeof server.env !== "object" || Array.isArray(server.env)
  )) throw new Error("BLUN language-guard MCP environment must be an object");
  if (server.args !== undefined && !Array.isArray(server.args)) {
    throw new Error("BLUN language-guard MCP arguments must be an array");
  }
  const env = server.env || {};
  const values = {
    command: String(server.command || ""),
    args: Array.isArray(server.args) ? server.args.map(String) : [],
    cwd: String(server.cwd || ""),
    endpoint: String(env.BLUN_LANGUAGE_GUARD_SERVICE_ENDPOINT || ""),
    tokenFile: String(env.BLUN_LANGUAGE_GUARD_SERVICE_TOKEN_FILE || ""),
  };
  if (
    !values.command || !values.endpoint || !values.tokenFile
    || values.command.length > 4096 || values.cwd.length > 4096
    || values.endpoint.length > 4096 || values.tokenFile.length > 4096
    || values.args.length > 128 || values.args.some(value => value.length > 4096)
  ) throw new Error("BLUN language-guard MCP entry is invalid");
  return {
    name: SERVER_NAME,
    ...values,
  };
}

function bootstrapLanguageGuardMcp({ userHome, store }) {
  const current = store.getAllInternal().find(item => String(item.name || "").toLowerCase() === SERVER_NAME);
  if (current) return { installed: false, id: current.id, reason: "already-installed" };
  let legacy;
  try { legacy = loadLegacyGuardConfig(userHome); }
  catch {
    throw new LanguageGuardBlocked("legacy BLUN MCP configuration is unsafe", "guard_unavailable");
  }
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
  readProtectedLegacyConfig,
  readProtectedServiceToken,
  bootstrapLanguageGuardMcp,
  resolveGuardConnection,
  resolveLanguage,
  languageFromMeta,
  createBlunLanguageGuard,
};
