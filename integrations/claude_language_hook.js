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
const MAX_RECORD_BYTES = 64 * 1024;
const MAX_EPOCH_BYTES = 128;
const MAX_SERVICE_TOKEN_BYTES = 64 * 1024;
const MAX_POLICY_BYTES = 64 * 1024;
const MAX_RECORD_AGE_MS = 10 * 60 * 1000;
const DEFAULT_RUNTIME = path.join(os.homedir(), ".config", "blun-language-guard");
const EXACT_LANGUAGE = /^(?:[A-Za-z]{2,8}|x)(?:-[A-Za-z0-9]{1,8})*$/;
const HTML_C1_NUMERIC_REFERENCE_REPLACEMENTS = new Map([
  [0x80, 0x20AC], [0x82, 0x201A], [0x83, 0x0192], [0x84, 0x201E],
  [0x85, 0x2026], [0x86, 0x2020], [0x87, 0x2021], [0x88, 0x02C6],
  [0x89, 0x2030], [0x8A, 0x0160], [0x8B, 0x2039], [0x8C, 0x0152],
  [0x8E, 0x017D], [0x91, 0x2018], [0x92, 0x2019], [0x93, 0x201C],
  [0x94, 0x201D], [0x95, 0x2022], [0x96, 0x2013], [0x97, 0x2014],
  [0x98, 0x02DC], [0x99, 0x2122], [0x9A, 0x0161], [0x9B, 0x203A],
  [0x9C, 0x0153], [0x9E, 0x017E], [0x9F, 0x0178]
]);
const NON_LANGUAGE_NAMED_REFERENCES = new Set([
  "AMP", "GT", "LT", "QUOT", "amp", "apos", "bull", "copy", "emsp", "ensp",
  "gt", "hairsp", "hellip", "laquo", "ldquo", "lrm", "lsquo", "lt", "mdash",
  "middot", "nbsp", "ndash", "quot", "raquo", "rdquo", "reg", "rlm", "rsquo",
  "shy", "thinsp", "trade", "zwj", "zwnj"
]);
const LEGACY_NON_LANGUAGE_NAMED_REFERENCES = [
  "brvbar", "divide", "frac12", "frac14", "frac34", "iquest", "middot", "plusmn",
  "pound", "acute", "curren", "iexcl", "laquo", "nbsp", "para", "raquo", "sect",
  "times", "cedil", "cent", "copy", "macr", "quot", "shy", "sup1", "sup2",
  "sup3", "AMP", "COPY", "QUOT", "REG", "amp", "deg", "GT", "gt", "LT", "lt",
  "not", "reg", "uml", "yen"
].sort((left, right) => right.length - left.length);
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

function containsLanguageCharacters(value) {
  const text = String(value || "");
  if (/\p{L}/u.test(text)) return true;
  const withoutEmojiFormatting = text.replace(/[\u20E3\uFE00-\uFE0F\u{E0100}-\u{E01EF}]/gu, "");
  return /\p{M}/u.test(withoutEmojiFormatting);
}

function hasNaturalLanguage(value) {
  let encodedNaturalLanguage = false;
  const textWithoutNumericReferences = String(value || "").replace(
    /&#(?:0*(\d{1,7})(?!\d)|x0*([0-9a-f]{1,6})(?![0-9a-f]));?/gi,
    (entity, decimal, hexadecimal) => {
      const parsedCodePoint = Number.parseInt(decimal || hexadecimal, decimal ? 10 : 16);
      if (Number.isSafeInteger(parsedCodePoint) && parsedCodePoint >= 0 && parsedCodePoint <= 0x10FFFF) {
        const renderedCodePoint = HTML_C1_NUMERIC_REFERENCE_REPLACEMENTS.get(parsedCodePoint)
          ?? parsedCodePoint;
        const decoded = String.fromCodePoint(renderedCodePoint);
        if (containsLanguageCharacters(decoded)) encodedNaturalLanguage = true;
      }
      return "";
    }
  );
  const text = textWithoutNumericReferences.replace(
    /&([A-Za-z][A-Za-z0-9]{1,31})(;?)/g,
    (entity, name, semicolon) => {
      if (semicolon && NON_LANGUAGE_NAMED_REFERENCES.has(name)) return "";
      const legacyReference = LEGACY_NON_LANGUAGE_NAMED_REFERENCES.find(
        (reference) => name.startsWith(reference)
      );
      if (!legacyReference) return entity;
      return `${name.slice(legacyReference.length)}${semicolon}`;
    }
  );
  return encodedNaturalLanguage || containsLanguageCharacters(text);
}

function validatePolicyStats(stats) {
  if (!stats.isFile() || stats.isSymbolicLink()) throw new Error("delivery policy must be a regular file");
  if (stats.nlink !== 1) throw new Error("delivery policy must not have additional hard links");
  if (stats.size < 2 || stats.size > MAX_POLICY_BYTES) throw new Error("delivery policy has an invalid size");
  if (process.platform !== "win32" && (stats.mode & 0o077) !== 0) {
    throw new Error("delivery policy permissions are too broad");
  }
  if (typeof process.getuid === "function" && stats.uid !== process.getuid()) {
    throw new Error("delivery policy has the wrong owner");
  }
}

function protectedDirectoryIdentity(stats) {
  return {
    dev: stats.dev,
    ino: stats.ino,
    mode: stats.mode,
    uid: stats.uid,
    gid: stats.gid
  };
}

function sameProtectedDirectoryIdentity(stats, expected) {
  return stats.dev === expected.dev
    && stats.ino === expected.ino
    && stats.mode === expected.mode
    && stats.uid === expected.uid
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

function ensureProtectedDirectory(directory, label) {
  const absoluteDirectory = path.resolve(directory);
  const home = path.resolve(os.homedir());
  const underHome = absoluteDirectory === home || absoluteDirectory.startsWith(`${home}${path.sep}`);
  const anchor = underHome ? home : existingProtectedDirectoryAnchor(absoluteDirectory, label);
  const relative = path.relative(anchor, absoluteDirectory);
  const components = relative ? relative.split(path.sep) : [];
  if (process.platform === "win32") {
    let current = anchor;
    validateProtectedDirectoryStats(fs.lstatSync(current), current, label);
    for (const component of components) {
      current = path.join(current, component);
      try {
        validateProtectedDirectoryStats(fs.lstatSync(current), current, label);
      } catch (error) {
        if (!error || error.code !== "ENOENT") throw error;
        try {
          fs.mkdirSync(current, { mode: 0o700 });
        } catch (mkdirError) {
          if (!mkdirError || mkdirError.code !== "EEXIST") throw mkdirError;
        }
        validateProtectedDirectoryStats(fs.lstatSync(current), current, label);
      }
    }
    return;
  }
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
      const accessPath = path.join(descriptorRoot, String(descriptor), component);
      let child;
      try {
        child = fs.openSync(accessPath, flags);
      } catch (error) {
        if (!error || error.code !== "ENOENT") throw error;
        try {
          fs.mkdirSync(accessPath, { mode: 0o700 });
        } catch (mkdirError) {
          if (!mkdirError || mkdirError.code !== "EEXIST") throw mkdirError;
        }
        child = fs.openSync(accessPath, flags);
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
    const held = fs.fstatSync(descriptor);
    const currentPath = fs.lstatSync(absoluteDirectory);
    validateProtectedDirectoryStats(currentPath, absoluteDirectory, label);
    if (!sameProtectedDirectoryIdentity(held, protectedDirectoryIdentity(currentPath))) {
      throw new Error(`${label} directory changed while creating: ${absoluteDirectory}`);
    }
  } catch (error) {
    if (error && String(error.message || "").startsWith(`${label} directory`)) throw error;
    throw new Error(`${label} directory cannot be created safely: ${current}`);
  } finally {
    if (descriptor !== null) fs.closeSync(descriptor);
  }
}

function openProtectedDirectory(file, label) {
  const absoluteFile = path.resolve(file);
  const directory = path.dirname(absoluteFile);
  if (process.platform === "win32") {
    const details = fs.lstatSync(directory);
    validateProtectedDirectoryStats(details, directory, label);
    return {
      accessPath: absoluteFile,
      absoluteFile,
      descriptor: null,
      directory,
      identity: protectedDirectoryIdentity(details)
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
      const child = fs.openSync(path.join(descriptorRoot, String(descriptor), component), flags);
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
    return { accessPath, absoluteFile, descriptor, directory, identity };
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
    if (!sameProtectedDirectoryIdentity(current, protectedDirectory.identity)) {
      throw new Error(`${label} directory changed while reading: ${protectedDirectory.directory}`);
    }
    return;
  }
  try {
    const held = fs.fstatSync(protectedDirectory.descriptor);
    const current = fs.lstatSync(protectedDirectory.directory);
    validateProtectedDirectoryStats(current, protectedDirectory.directory, label);
    if (!sameProtectedDirectoryIdentity(held, protectedDirectory.identity)
        || !sameProtectedDirectoryIdentity(current, protectedDirectory.identity)) {
      throw new Error(`${label} directory changed while reading: ${protectedDirectory.directory}`);
    }
  } finally {
    fs.closeSync(protectedDirectory.descriptor);
  }
}

function readProtectedDeliveryPolicy(file) {
  const protectedDirectory = openProtectedDirectory(file, "delivery policy");
  const policyFile = protectedDirectory.accessPath;
  try {
    const before = fs.lstatSync(policyFile);
    validatePolicyStats(before);
    const noFollow = typeof fs.constants.O_NOFOLLOW === "number" ? fs.constants.O_NOFOLLOW : 0;
    const descriptor = fs.openSync(policyFile, fs.constants.O_RDONLY | noFollow);
    let raw;
    let opened;
    try {
      opened = fs.fstatSync(descriptor);
      validatePolicyStats(opened);
      if (!sameRecordIdentity(opened, recordIdentity(before)) || opened.nlink !== before.nlink) {
        throw new Error("delivery policy changed while opening");
      }
      const buffer = Buffer.alloc(MAX_POLICY_BYTES + 1);
      let size = 0;
      while (size < buffer.length) {
        const count = fs.readSync(descriptor, buffer, size, buffer.length - size, null);
        if (count === 0) break;
        size += count;
      }
      const afterRead = fs.fstatSync(descriptor);
      if (!sameRecordIdentity(afterRead, recordIdentity(opened)) || afterRead.nlink !== opened.nlink) {
        throw new Error("delivery policy changed while reading");
      }
      if (size > MAX_POLICY_BYTES) throw new Error("delivery policy has an invalid size");
      raw = new TextDecoder("utf-8", { fatal: true }).decode(buffer.subarray(0, size)).replace(/^\uFEFF/, "");
    } finally {
      fs.closeSync(descriptor);
    }
    const afterPath = fs.lstatSync(policyFile);
    if (!sameRecordIdentity(afterPath, recordIdentity(opened)) || afterPath.nlink !== opened.nlink) {
      throw new Error("delivery policy changed while reading");
    }
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("delivery policy root must be an object");
    }
    const isolated = parsed.isolated_service;
    if (parsed.mandatory !== true || !isolated || typeof isolated !== "object" || Array.isArray(isolated)
        || isolated.required !== true || typeof isolated.endpoint !== "string" || !isolated.endpoint.trim()
        || isolated.endpoint.length > 4096 || typeof isolated.token_file !== "string" || !isolated.token_file.trim()
        || isolated.token_file.length > 4096
        || (Object.hasOwn(parsed, "fail_closed") && parsed.fail_closed !== true)
        || (Object.hasOwn(parsed, "direct_delivery_allowed") && parsed.direct_delivery_allowed !== false)
        || (Object.hasOwn(parsed, "raw_streaming_allowed") && parsed.raw_streaming_allowed !== false)
        || (Object.hasOwn(parsed, "on_guard_error") && parsed.on_guard_error !== "block")) {
      throw new Error("mandatory isolated-service policy is invalid");
    }
    return parsed;
  } finally {
    closeProtectedDirectory(protectedDirectory, "delivery policy");
  }
}

function validateServiceTokenStats(stats) {
  if (!stats.isFile() || stats.isSymbolicLink()) throw new Error("service token must be a regular file");
  if (stats.nlink !== 1) throw new Error("service token must not have additional hard links");
  if (stats.size < 32 || stats.size > MAX_SERVICE_TOKEN_BYTES) throw new Error("service token has an invalid size");
  if (process.platform !== "win32" && (stats.mode & 0o077) !== 0) {
    throw new Error("service token permissions are too broad");
  }
  if (typeof process.getuid === "function" && stats.uid !== process.getuid()) {
    throw new Error("service token has the wrong owner");
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
    let opened;
    try {
      opened = fs.fstatSync(descriptor);
      validateServiceTokenStats(opened);
      if (!sameRecordIdentity(opened, recordIdentity(before)) || opened.nlink !== before.nlink) {
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
      if (!sameRecordIdentity(after, recordIdentity(opened)) || after.nlink !== opened.nlink
          || !sameRecordIdentity(afterPath, recordIdentity(opened)) || afterPath.nlink !== opened.nlink) {
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

function runtimeConfig() {
  const runtime = process.env.BLUN_LANGUAGE_GUARD_RUNTIME || DEFAULT_RUNTIME;
  const policyPath = process.env.BLUN_LANGUAGE_GUARD_POLICY || path.join(runtime, "delivery-policy.json");
  const policy = readProtectedDeliveryPolicy(policyPath);
  const isolated = policy.isolated_service;
  if (policy.mandatory !== true || !isolated || isolated.required !== true) {
    throw new Error("mandatory isolated-service policy is missing");
  }
  const endpoint = process.env.BLUN_LANGUAGE_GUARD_SERVICE_ENDPOINT || isolated.endpoint;
  const tokenFile = process.env.BLUN_LANGUAGE_GUARD_SERVICE_TOKEN_FILE || isolated.token_file;
  const token = process.env.BLUN_LANGUAGE_GUARD_SERVICE_TOKEN || readProtectedServiceToken(tokenFile);
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

function validateSessionEpochStats(stats) {
  if (!stats.isFile() || stats.isSymbolicLink()) throw new Error("session epoch must be a regular file");
  if (stats.nlink !== 1) throw new Error("session epoch must not have additional hard links");
  if (stats.size < 1 || stats.size > MAX_EPOCH_BYTES) throw new Error("session epoch has an invalid size");
  if (process.platform !== "win32" && (stats.mode & 0o077) !== 0) {
    throw new Error("session epoch permissions are too broad");
  }
  if (typeof process.getuid === "function" && stats.uid !== process.getuid()) {
    throw new Error("session epoch has the wrong owner");
  }
}

function readSessionEpochFile(epochFile) {
  const before = fs.lstatSync(epochFile);
  validateSessionEpochStats(before);
  const noFollow = typeof fs.constants.O_NOFOLLOW === "number" ? fs.constants.O_NOFOLLOW : 0;
  const descriptor = fs.openSync(epochFile, fs.constants.O_RDONLY | noFollow);
  try {
    const opened = fs.fstatSync(descriptor);
    validateSessionEpochStats(opened);
    if (!sameRecordIdentity(opened, recordIdentity(before))) {
      throw new Error("session epoch changed while opening");
    }
    const rawEpoch = fs.readFileSync(descriptor, "utf8");
    const finished = fs.fstatSync(descriptor);
    validateSessionEpochStats(finished);
    if (!sameRecordIdentity(finished, recordIdentity(opened))) {
      throw new Error("session epoch changed while reading");
    }
    const epoch = rawEpoch.replace(/^\uFEFF/, "").trim();
    if (!/^[a-f0-9]{64}$/.test(epoch)) throw new Error("session epoch is invalid");
    return { epoch, fileIdentity: recordIdentity(opened) };
  } finally {
    fs.closeSync(descriptor);
  }
}

function readSessionEpoch(input) {
  const destination = sessionEpochPath(input);
  const protectedDirectory = openProtectedDirectory(destination, "session epoch");
  try {
    return { destination, ...readSessionEpochFile(protectedDirectory.accessPath) };
  } finally {
    closeProtectedDirectory(protectedDirectory, "session epoch");
  }
}

function removeExistingSessionEpochFile(epochFile) {
  let inspected;
  try {
    inspected = readSessionEpochFile(epochFile);
  } catch (error) {
    if (error && error.code === "ENOENT") return;
    throw error;
  }
  quarantineAndRemoveExactFile(
    epochFile,
    inspected.fileIdentity,
    validateSessionEpochStats,
    "session epoch changed before renewal",
    "session epoch changed while quarantining"
  );
}

function assertSessionEpochPublicationTargetAbsent(epochFile) {
  let current;
  try {
    current = fs.lstatSync(epochFile);
  } catch (error) {
    if (error && error.code === "ENOENT") return;
    throw error;
  }
  validateSessionEpochStats(current);
  throw new Error("session epoch changed before publication");
}

async function beginSessionEpoch(input) {
  const directory = stateDirectory();
  ensureProtectedDirectory(directory, "session epoch");
  const destination = sessionEpochPath(input);
  const protectedDirectory = openProtectedDirectory(destination, "session epoch");
  const epochFile = protectedDirectory.accessPath;
  try {
    removeExistingSessionEpochFile(epochFile);
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
    const temporary = `${epochFile}.${process.pid}.${crypto.randomBytes(12).toString("hex")}.tmp`;
    let descriptor;
    let createdIdentity;
    try {
      descriptor = fs.openSync(temporary, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL, 0o600);
      createdIdentity = temporaryFileIdentity(fs.fstatSync(descriptor));
      fs.writeFileSync(descriptor, `${epoch}\n`, "utf8");
      if (process.platform !== "win32") fs.fchmodSync(descriptor, 0o600);
      fs.fsyncSync(descriptor);
      const sealedStats = fs.fstatSync(descriptor);
      validateSessionEpochStats(sealedStats);
      const sealedIdentity = recordIdentity(sealedStats);
      assertSessionEpochPublicationTargetAbsent(epochFile);
      assertProtectedPublicationFile(
        temporary,
        sealedIdentity,
        validateSessionEpochStats,
        "session epoch temporary file changed before publication"
      );
      fs.renameSync(temporary, epochFile);
      const publishedStats = fs.fstatSync(descriptor);
      validateSessionEpochStats(publishedStats);
      assertProtectedPublicationFile(
        epochFile,
        recordIdentity(publishedStats),
        validateSessionEpochStats,
        "session epoch changed during publication"
      );
      fs.closeSync(descriptor);
      descriptor = undefined;
    } finally {
      if (descriptor !== undefined) fs.closeSync(descriptor);
      removeCreatedTemporaryFile(temporary, createdIdentity, "session epoch");
    }
    return epoch;
  } finally {
    closeProtectedDirectory(protectedDirectory, "session epoch");
  }
}

function statePath(input) {
  return path.join(stateDirectory(), `${identity(input)}.json`);
}

function validateRecordStats(stats) {
  if (!stats.isFile() || stats.isSymbolicLink()) {
    throw new Error("delivery grant state must be a regular file");
  }
  if (stats.nlink !== 1) {
    throw new Error("delivery grant state must not have additional hard links");
  }
  if (stats.size < 1 || stats.size > MAX_RECORD_BYTES) {
    throw new Error("delivery grant state has an invalid size");
  }
  if (process.platform !== "win32" && (stats.mode & 0o077) !== 0) {
    throw new Error("delivery grant state permissions are too broad");
  }
  if (typeof process.getuid === "function" && stats.uid !== process.getuid()) {
    throw new Error("delivery grant state has the wrong owner");
  }
}

function recordIdentity(stats) {
  return {
    dev: stats.dev,
    ino: stats.ino,
    nlink: stats.nlink,
    size: stats.size,
    ctimeMs: stats.ctimeMs,
    mtimeMs: stats.mtimeMs
  };
}

function sameRecordIdentity(stats, expected) {
  return expected && stats.dev === expected.dev && stats.ino === expected.ino
    && stats.nlink === expected.nlink && stats.size === expected.size && stats.ctimeMs === expected.ctimeMs
    && stats.mtimeMs === expected.mtimeMs;
}

function sameRenamedRecordIdentity(stats, expected) {
  return expected && stats.dev === expected.dev && stats.ino === expected.ino
    && stats.nlink === expected.nlink && stats.size === expected.size && stats.mtimeMs === expected.mtimeMs;
}

function quarantineAndRemoveExactFile(file, expected, validate, changedBeforeMessage, changedDuringMessage) {
  const current = fs.lstatSync(file);
  validate(current);
  if (!sameRecordIdentity(current, expected)) throw new Error(changedBeforeMessage);
  const noFollow = typeof fs.constants.O_NOFOLLOW === "number" ? fs.constants.O_NOFOLLOW : 0;
  const descriptor = fs.openSync(file, fs.constants.O_RDONLY | noFollow);
  const quarantine = `${file}.${process.pid}.${crypto.randomBytes(12).toString("hex")}.remove`;
  try {
    const opened = fs.fstatSync(descriptor);
    validate(opened);
    if (!sameRecordIdentity(opened, expected)) throw new Error(changedBeforeMessage);
    try {
      fs.lstatSync(quarantine);
      throw new Error(`${changedDuringMessage}: quarantine target already exists`);
    } catch (error) {
      if (!error || error.code !== "ENOENT") throw error;
    }
    fs.renameSync(file, quarantine);
    const held = fs.fstatSync(descriptor);
    const moved = fs.lstatSync(quarantine);
    validate(held);
    validate(moved);
    if (!sameRenamedRecordIdentity(held, expected)
        || !sameRecordIdentity(moved, recordIdentity(held))) {
      throw new Error(changedDuringMessage);
    }
    fs.unlinkSync(quarantine);
    const removed = fs.fstatSync(descriptor);
    if (removed.nlink !== 0) throw new Error(changedDuringMessage);
  } finally {
    fs.closeSync(descriptor);
  }
}

function temporaryFileIdentity(stats) {
  return {
    dev: stats.dev,
    ino: stats.ino,
    nlink: stats.nlink,
    birthtimeMs: stats.birthtimeMs
  };
}

function sameTemporaryFileIdentity(stats, expected) {
  return expected && stats.dev === expected.dev && stats.ino === expected.ino
    && stats.nlink === expected.nlink && stats.birthtimeMs === expected.birthtimeMs;
}

function assertProtectedPublicationFile(file, expected, validate, message) {
  const current = fs.lstatSync(file);
  validate(current);
  if (!sameRecordIdentity(current, expected)) throw new Error(message);
}

function removeCreatedTemporaryFile(file, expected, label) {
  if (!expected) return;
  let current;
  try {
    current = fs.lstatSync(file);
  } catch (error) {
    if (error && error.code === "ENOENT") return;
    throw error;
  }
  if (!current.isFile() || current.isSymbolicLink() || current.nlink !== 1
      || (typeof process.getuid === "function" && current.uid !== process.getuid())
      || !sameTemporaryFileIdentity(current, expected)) {
    throw new Error(`${label} temporary file changed before cleanup`);
  }
  fs.unlinkSync(file);
}

function readProtectedRecordFile(recordFile) {
  const before = fs.lstatSync(recordFile);
  validateRecordStats(before);
  const noFollow = typeof fs.constants.O_NOFOLLOW === "number" ? fs.constants.O_NOFOLLOW : 0;
  const descriptor = fs.openSync(recordFile, fs.constants.O_RDONLY | noFollow);
  try {
    const opened = fs.fstatSync(descriptor);
    validateRecordStats(opened);
    if (!sameRecordIdentity(opened, recordIdentity(before))) {
      throw new Error("delivery grant state changed while opening");
    }
    const raw = fs.readFileSync(descriptor, "utf8").replace(/^\uFEFF/, "");
    const finished = fs.fstatSync(descriptor);
    validateRecordStats(finished);
    if (!sameRecordIdentity(finished, recordIdentity(opened))) {
      throw new Error("delivery grant state changed while reading");
    }
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("delivery grant state root must be an object");
    }
    return { record: parsed, fileIdentity: recordIdentity(opened) };
  } finally {
    fs.closeSync(descriptor);
  }
}

function readProtectedRecord(destination) {
  const protectedDirectory = openProtectedDirectory(destination, "delivery grant state");
  try {
    return readProtectedRecordFile(protectedDirectory.accessPath);
  } finally {
    closeProtectedDirectory(protectedDirectory, "delivery grant state");
  }
}

function removeExactRecordFile(stateFile, expected) {
  quarantineAndRemoveExactFile(
    stateFile,
    expected,
    validateRecordStats,
    "delivery grant state changed before consumption",
    "delivery grant state changed while quarantining"
  );
}

function removeExactRecord(destination, expected) {
  const protectedDirectory = openProtectedDirectory(destination, "Claude hook state");
  try {
    removeExactRecordFile(protectedDirectory.accessPath, expected);
  } finally {
    closeProtectedDirectory(protectedDirectory, "Claude hook state");
  }
}

function inspectRecordPublicationTarget(stateFile) {
  try {
    return readProtectedRecordFile(stateFile).fileIdentity;
  } catch (error) {
    if (error && error.code === "ENOENT") return null;
    throw error;
  }
}

function assertRecordPublicationTarget(stateFile, expected) {
  let current;
  try {
    current = fs.lstatSync(stateFile);
  } catch (error) {
    if (error && error.code === "ENOENT" && expected === null) return;
    if (error && error.code === "ENOENT") {
      throw new Error("delivery grant state changed before publication");
    }
    throw error;
  }
  validateRecordStats(current);
  if (expected === null || !sameRecordIdentity(current, expected)) {
    throw new Error("delivery grant state changed before publication");
  }
}

function writeRecord(input, record) {
  const destination = statePath(input);
  const protectedDirectory = openProtectedDirectory(destination, "delivery grant state");
  const stateFile = protectedDirectory.accessPath;
  const temporary = `${stateFile}.${process.pid}.${crypto.randomBytes(12).toString("hex")}.tmp`;
  try {
    const existingIdentity = inspectRecordPublicationTarget(stateFile);
    let descriptor;
    let createdIdentity;
    try {
      descriptor = fs.openSync(temporary, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL, 0o600);
      createdIdentity = temporaryFileIdentity(fs.fstatSync(descriptor));
      fs.writeFileSync(descriptor, `${JSON.stringify(record)}\n`, "utf8");
      if (process.platform !== "win32") fs.fchmodSync(descriptor, 0o600);
      fs.fsyncSync(descriptor);
      const sealedStats = fs.fstatSync(descriptor);
      validateRecordStats(sealedStats);
      const sealedIdentity = recordIdentity(sealedStats);
      assertRecordPublicationTarget(stateFile, existingIdentity);
      assertProtectedPublicationFile(
        temporary,
        sealedIdentity,
        validateRecordStats,
        "delivery grant temporary file changed before publication"
      );
      fs.renameSync(temporary, stateFile);
      const publishedStats = fs.fstatSync(descriptor);
      validateRecordStats(publishedStats);
      assertProtectedPublicationFile(
        stateFile,
        recordIdentity(publishedStats),
        validateRecordStats,
        "delivery grant state changed during publication"
      );
      fs.closeSync(descriptor);
      descriptor = undefined;
    } finally {
      if (descriptor !== undefined) fs.closeSync(descriptor);
      removeCreatedTemporaryFile(temporary, createdIdentity, "delivery grant");
    }
  } finally {
    closeProtectedDirectory(protectedDirectory, "delivery grant state");
  }
}

function readRecord(input) {
  const destination = statePath(input);
  try {
    return { destination, ...readProtectedRecord(destination) };
  } catch (error) {
    if (error && error.code === "ENOENT") return { destination, record: null, fileIdentity: null };
    throw error;
  }
}

function invalidateAgentRecord(input) {
  const destination = statePath(input);
  const protectedDirectory = openProtectedDirectory(destination, "delivery grant state");
  const stateFile = protectedDirectory.accessPath;
  try {
    try {
      const { fileIdentity } = readProtectedRecordFile(stateFile);
      removeExactRecordFile(stateFile, fileIdentity);
    } catch (error) {
      if (!error || error.code !== "ENOENT") throw error;
    }
  } finally {
    closeProtectedDirectory(protectedDirectory, "delivery grant state");
  }
}

function invalidateSessionRecords(input) {
  const directory = stateDirectory();
  const expectedSession = sessionHash(input);
  const legacyMainRecordName = path.basename(statePath(input));
  try {
    fs.lstatSync(directory);
  } catch (error) {
    if (error && error.code === "ENOENT") return;
    throw error;
  }
  const anchor = path.join(directory, ".session-invalidation-anchor");
  const protectedDirectory = openProtectedDirectory(anchor, "Claude hook state");
  const stateAccessDirectory = path.dirname(protectedDirectory.accessPath);
  try {
    const entries = fs.readdirSync(stateAccessDirectory, { withFileTypes: true });
    for (const entry of entries) {
      if ((!entry.isFile() && !entry.isSymbolicLink()) || !entry.name.endsWith(".json")) continue;
      const candidate = path.join(stateAccessDirectory, entry.name);
      const { record, fileIdentity } = readProtectedRecordFile(candidate);
      const legacyGrant = typeof record.session_sha256 !== "string"
        && typeof record.delivery_grant === "string"
        && Number.isFinite(record.authorized_at);
      const belongsToSession = entry.name === legacyMainRecordName
        || record.session_sha256 === expectedSession || legacyGrant;
      if (!belongsToSession) continue;
      removeExactRecordFile(candidate, fileIdentity);
    }
  } finally {
    closeProtectedDirectory(protectedDirectory, "Claude hook state");
  }
}

function emit(payload) {
  if (payload) process.stdout.write(`${JSON.stringify(payload)}\n`);
}

function blocked(reason) {
  return { decision: "block", reason };
}

function hostReleasePolicy(environment = process.env) {
  const hasLanguage = Object.prototype.hasOwnProperty.call(environment, "BLUN_LANGUAGE_GUARD_LANGUAGE");
  const hasTaskKind = Object.prototype.hasOwnProperty.call(environment, "BLUN_LANGUAGE_GUARD_TASK_KIND");
  if (!hasLanguage && !hasTaskKind) return null;
  const language = hasLanguage ? String(environment.BLUN_LANGUAGE_GUARD_LANGUAGE || "").trim() : "";
  const taskKind = hasTaskKind ? String(environment.BLUN_LANGUAGE_GUARD_TASK_KIND || "").trim().toLowerCase() : "";
  if (hasLanguage && (!EXACT_LANGUAGE.test(language) || ["auto", "all"].includes(language.toLowerCase()))) {
    throw new Error("host release language must be an exact language or locale tag");
  }
  if (hasTaskKind && !["response", "translation"].includes(taskKind)) {
    throw new Error("host release task kind must be response or translation");
  }
  return { language, taskKind };
}

function hostPolicyInstruction() {
  let policy;
  try {
    policy = hostReleasePolicy();
  } catch (_) {
    return "The trusted host release policy is invalid. Fail closed: do not call a release tool, finish, or deliver natural-language output until the host policy is repaired.";
  }
  if (!policy) return "";
  const parts = [];
  if (policy.taskKind) {
    const tool = policy.taskKind === "translation" ? "release_translation" : "release_response";
    parts.push(`The trusted host requires ${tool}; do not use the other release purpose.`);
  }
  if (policy.language) {
    parts.push(`Pass language exactly as ${JSON.stringify(policy.language)}. Do not substitute a base language or another locale.`);
  }
  return parts.join(" ");
}

function preTool(input) {
  const toolName = String(input.tool_name || "");
  const purpose = toolName.endsWith("__release_translation") ? "translation"
    : toolName.endsWith("__release_response") ? "response" : "";
  if (!purpose) return;
  let policy;
  try {
    policy = hostReleasePolicy();
  } catch (error) {
    emit({
      hookSpecificOutput: {
        hookEventName: "PreToolUse",
        permissionDecision: "deny",
        permissionDecisionReason: `BLUN Language Guard blocked an invalid host release policy: ${error.message}.`,
      }
    });
    return;
  }
  if (!policy) return;
  if (policy.taskKind && policy.taskKind !== purpose) {
    const required = policy.taskKind === "translation" ? "release_translation" : "release_response";
    emit({
      hookSpecificOutput: {
        hookEventName: "PreToolUse",
        permissionDecision: "deny",
        permissionDecisionReason: `The trusted host requires ${required} for this turn.`,
      }
    });
    return;
  }
  if (!policy.language) return;
  const toolInput = input.tool_input && typeof input.tool_input === "object" && !Array.isArray(input.tool_input)
    ? input.tool_input : {};
  emit({
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      updatedInput: { ...toolInput, language: policy.language },
      additionalContext: `The trusted host fixed this release to language ${JSON.stringify(policy.language)}.`,
    }
  });
}

function isDirectTelegramDeliveryTool(toolName) {
  return /^mcp__.*telegram.*__(?:reply|send|send_message|sendMessage)$/i.test(String(toolName || ""));
}

function preDelivery(input) {
  if (!isDirectTelegramDeliveryTool(input.tool_name)) return;
  emit({
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: "Direct Telegram delivery is disabled by mandatory BLUN Translate Native. Return the verified final response normally; the host-owned bridge must deliver it after Stop verification."
    }
  });
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
  const policyInstruction = hostPolicyInstruction();
  if (!healthy) {
    return `${subject} is protected by mandatory BLUN Translate Native, but its isolated guard is unavailable. Fail closed: do not finish or deliver natural-language output until the service is healthy.${policyInstruction ? ` ${policyInstruction}` : ""}`;
  }
  return `${subject} is protected by mandatory BLUN Translate Native. Before every natural-language final answer call release_response with the exact final text. For translations load the translate-native skill and call release_translation with the complete source and target. Never call a Telegram reply or send tool directly; after Stop verifies the exact final response, the host-owned bridge delivers it. Do not rely on another agent's release: the final visible text must use a fresh grant bound to this session and agent identity and remain byte-for-byte equivalent after Unicode normalization to the released target.${policyInstruction ? ` ${policyInstruction}` : ""}`;
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
    return;
  }
  const policyInstruction = hostPolicyInstruction();
  if (policyInstruction) {
    emit({
      hookSpecificOutput: {
        hookEventName: "UserPromptSubmit",
        additionalContext: policyInstruction,
      }
    });
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

async function sessionEnd(input) {
  let previousEpoch = "";
  try {
    const current = readSessionEpoch(input);
    previousEpoch = current.epoch;
    removeExactRecord(current.destination, current.fileIdentity);
  } catch (_) {}
  try { invalidateSessionRecords(input); } catch (_) {}
  if (!previousEpoch) return;
  try {
    await callGuard({
      operation: "retire_session_epoch",
      session_id: String(input.session_id || ""),
      session_epoch: previousEpoch
    }, 700);
  } catch (_) {
    // SessionEnd cannot block termination. Local authority is already gone;
    // a later SessionStart must establish a fresh service-authoritative epoch.
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
  let policy;
  try {
    policy = hostReleasePolicy();
  } catch (_) {
    rejectRelease(input, "The trusted host release policy is invalid. Repair it before releasing or delivering natural-language output.");
    return;
  }
  if (policy?.taskKind && policy.taskKind !== purpose) {
    const required = policy.taskKind === "translation" ? "release_translation" : "release_response";
    rejectRelease(input, `The trusted host requires ${required} for this turn. Call that release tool with a fresh candidate.`);
    return;
  }
  if (policy?.language && policy.language !== language) {
    rejectRelease(input, `The release used the wrong language tag. Retry with language exactly ${JSON.stringify(policy.language)}.`);
    return;
  }
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
  if (!input || typeof input.last_assistant_message !== "string") {
    emit(blockedStop(input, "The BLUN hook received no valid last_assistant_message and cannot verify the actual final response. Fail closed and retry after Claude supplies the documented Stop output field."));
    return;
  }
  const target = input.last_assistant_message;
  const naturalLanguage = hasNaturalLanguage(target);
  try {
    const { destination, record, fileIdentity } = readRecord(input);
    const { epoch: sessionEpoch } = readSessionEpoch(input);
    const fresh = record && Number.isFinite(record.authorized_at)
      && Date.now() - record.authorized_at >= 0
      && Date.now() - record.authorized_at <= MAX_RECORD_AGE_MS;
    const usable = fresh && typeof record.delivery_grant === "string"
      && record.session_epoch_sha256 === textHash(sessionEpoch);
    if (record) removeExactRecord(destination, fileIdentity);
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
  if (mode === "session-end") return sessionEnd(input);
  if (mode === "pre-delivery") return preDelivery(input);
  if (mode === "pre-tool") return preTool(input);
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

module.exports = { beginSessionEpoch, blockedStop, canonicalText, findRelease, hasNaturalLanguage, hostReleasePolicy, invalidateAgentRecord, invalidateSessionRecords, isDirectTelegramDeliveryTool, postToolFailure, preDelivery, preTool, readProtectedDeliveryPolicy, readProtectedRecord, readProtectedServiceToken, readSessionEpoch, removeExactRecord, sessionEnd, sessionHash, stopFailure, textHash, writeRecord };
