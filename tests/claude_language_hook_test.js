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
const { beginSessionEpoch, hasNaturalLanguage, invalidateAgentRecord, invalidateSessionRecords, readProtectedDeliveryPolicy, readProtectedRecord, readProtectedServiceToken, readSessionEpoch, removeExactRecord, writeRecord } = require(HOOK);

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
  assert(hasNaturalLanguage("&#78;&#97;&#116;&#252;&#114;&#108;&#105;&#99;&#104;"));
  assert(
    hasNaturalLanguage("&#78 &#97 &#116 &#252 &#114 &#108 &#105 &#99 &#104"),
    "semicolonless decimal language entities must require verification"
  );
  assert(
    hasNaturalLanguage("&#00000078;&#00000097;&#00000116;&#00000252;&#00000114;&#00000108;&#00000105;&#00000099;&#00000104;"),
    "zero-padded decimal language entities must require verification"
  );
  assert(hasNaturalLanguage("\u0308"), "a standalone combining mark must require verification");
  assert(hasNaturalLanguage("&#776;"), "an encoded combining mark must require verification");
  assert(hasNaturalLanguage("&#776"), "a semicolonless combining mark must require verification");
  assert(
    hasNaturalLanguage("&#x8A;&#140;&#x9E;&#159;"),
    "HTML C1 references that render as letters must require verification"
  );
  assert(
    hasNaturalLanguage("&#00000138 &#00000154"),
    "zero-padded semicolonless HTML C1 letter references must require verification"
  );
  assert(
    !hasNaturalLanguage("&#128;&#130;&#132;&#133;&#134;&#135;&#137;&#145;&#146;&#147;&#148;&#149;&#150;&#151;&#153;"),
    "HTML C1 references that render only punctuation or symbols must remain allowed"
  );
  assert(!hasNaturalLanguage("&#99999999"), "an overlong decimal entity must not be decoded in parts");
  assert(!hasNaturalLanguage("&#128512;"), "a decimal emoji entity must not become a language false positive");
  assert(!hasNaturalLanguage("&#x1F600;"), "a hexadecimal emoji entity must not become a language false positive");
  for (const emoji of ["❤️", "☹️", "✈️", "1️⃣", "&#10084;&#65039;", "&#49;&#65039;&#8419;", "&#49 &#65039 &#8419", "&#00000049;&#000065039;&#000008419;"]) {
    assert(!hasNaturalLanguage(emoji), `${emoji} must remain an emoji-only output`);
  }
  assert(
    !hasNaturalLanguage("&nbsp;&amp;&hellip;&mdash;&copy;"),
    "named spacing, punctuation, and symbol references must not become language false positives"
  );
  assert(
    !hasNaturalLanguage("&nbsp&amp&copy&reg&gt&lt&quot&frac12&times"),
    "legacy non-language references without semicolons must not become language false positives"
  );
  assert(!hasNaturalLanguage("&nbsp123"), "numeric suffixes after legacy references must remain allowed");
  for (const linguisticEntity of ["&Auml;", "&aring;", "&unknown;", "&Auml", "&aring", "&hellip", "&notin", "&nbspText"]) {
    assert(hasNaturalLanguage(linguisticEntity), `${linguisticEntity} must remain fail-closed`);
  }

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

  const lateSwapRecord = path.join(temporary, "late-swap-record.json");
  const lateSwapOriginal = `${lateSwapRecord}.old`;
  const lateSwapReplacement = `${lateSwapRecord}.replacement`;
  fs.writeFileSync(lateSwapRecord, '{"grant":"original"}\n', { mode: 0o600 });
  fs.writeFileSync(lateSwapReplacement, '{"grant":"replacement"}\n', { mode: 0o600 });
  const inspectedLateSwapRecord = readProtectedRecord(lateSwapRecord);
  const originalLateSwapRenameSync = fs.renameSync;
  let exchangedLateSwapRecord = false;
  fs.renameSync = (source, destination, ...options) => {
    if (!exchangedLateSwapRecord && path.basename(String(source)) === path.basename(lateSwapRecord)
        && String(destination).endsWith(".remove")) {
      originalLateSwapRenameSync(lateSwapRecord, lateSwapOriginal);
      originalLateSwapRenameSync(lateSwapReplacement, lateSwapRecord);
      exchangedLateSwapRecord = true;
    }
    return originalLateSwapRenameSync(source, destination, ...options);
  };
  try {
    assert.throws(
      () => removeExactRecord(lateSwapRecord, inspectedLateSwapRecord.fileIdentity),
      /changed while quarantining/
    );
  } finally {
    fs.renameSync = originalLateSwapRenameSync;
  }
  assert(exchangedLateSwapRecord, "the grant must be exchanged at the final removal boundary");
  const quarantinedLateSwapRecord = fs.readdirSync(temporary)
    .find((entry) => entry.startsWith(`${path.basename(lateSwapRecord)}.`) && entry.endsWith(".remove"));
  assert(quarantinedLateSwapRecord, "the exchanged grant must remain quarantined instead of being deleted");
  assert.strictEqual(fs.readFileSync(lateSwapOriginal, "utf8"), '{"grant":"original"}\n');
  assert.strictEqual(
    fs.readFileSync(path.join(temporary, quarantinedLateSwapRecord), "utf8"),
    '{"grant":"replacement"}\n'
  );
  if (process.platform !== "win32") {
    const hardlinkedRecord = path.join(temporary, "hardlinked-record.json");
    const hardlinkedRecordAlias = `${hardlinkedRecord}.alias`;
    fs.writeFileSync(hardlinkedRecord, '{"grant":"hardlinked"}\n', { mode: 0o600 });
    fs.linkSync(hardlinkedRecord, hardlinkedRecordAlias);
    assert.throws(() => readProtectedRecord(hardlinkedRecord), /hard links/);
    fs.unlinkSync(hardlinkedRecordAlias);
    const inspectedHardlinkCandidate = readProtectedRecord(hardlinkedRecord);
    fs.linkSync(hardlinkedRecord, hardlinkedRecordAlias);
    assert.throws(
      () => removeExactRecord(hardlinkedRecord, inspectedHardlinkCandidate.fileIdentity),
      /hard links/
    );
    assert(fs.existsSync(hardlinkedRecord), "a hard-linked grant must not be consumed");
    assert(fs.existsSync(hardlinkedRecordAlias), "the grant alias must remain untouched");
    fs.unlinkSync(hardlinkedRecordAlias);
    fs.unlinkSync(hardlinkedRecord);

    const recordName = "delivery-grant.json";
    const linkedRecordTarget = path.join(temporary, "linked-record-target");
    const linkedRecordDirectory = path.join(temporary, "linked-record-directory");
    fs.mkdirSync(linkedRecordTarget, { mode: 0o700 });
    fs.writeFileSync(path.join(linkedRecordTarget, recordName), '{"grant":"linked"}\n', { mode: 0o600 });
    fs.symlinkSync(linkedRecordTarget, linkedRecordDirectory, "dir");
    assert.throws(
      () => readProtectedRecord(path.join(linkedRecordDirectory, recordName)),
      /directory cannot be opened safely/
    );

    const writableRecordDirectory = path.join(temporary, "writable-record-directory");
    fs.mkdirSync(writableRecordDirectory, { mode: 0o700 });
    fs.writeFileSync(path.join(writableRecordDirectory, recordName), '{"grant":"writable"}\n', { mode: 0o600 });
    fs.chmodSync(writableRecordDirectory, 0o777);
    try {
      assert.throws(
        () => readProtectedRecord(path.join(writableRecordDirectory, recordName)),
        /directory is writable outside its owner/
      );
    } finally {
      fs.chmodSync(writableRecordDirectory, 0o700);
    }

    const trustedRecordDirectory = path.join(temporary, "trusted-record-directory");
    const replacementRecordDirectory = path.join(temporary, "replacement-record-directory");
    fs.mkdirSync(trustedRecordDirectory, { mode: 0o700 });
    fs.mkdirSync(replacementRecordDirectory, { mode: 0o700 });
    const trustedRecord = path.join(trustedRecordDirectory, recordName);
    fs.writeFileSync(trustedRecord, '{"grant":"original"}\n', { mode: 0o600 });
    fs.writeFileSync(
      path.join(replacementRecordDirectory, recordName),
      '{"grant":"replacement"}\n',
      { mode: 0o600 }
    );
    const originalRecordLstatSync = fs.lstatSync;
    const originalRecordOpenSync = fs.openSync;
    let exchangedRecordDirectory = false;
    let restoredRecordDirectory = false;
    fs.lstatSync = (candidate, ...options) => {
      const details = originalRecordLstatSync(candidate, ...options);
      if (path.basename(String(candidate)) === recordName && !exchangedRecordDirectory) {
        fs.renameSync(trustedRecordDirectory, `${trustedRecordDirectory}-old`);
        fs.renameSync(replacementRecordDirectory, trustedRecordDirectory);
        exchangedRecordDirectory = true;
      }
      return details;
    };
    fs.openSync = (candidate, ...options) => {
      const descriptor = originalRecordOpenSync(candidate, ...options);
      if (path.basename(String(candidate)) === recordName
          && exchangedRecordDirectory && !restoredRecordDirectory) {
        fs.renameSync(trustedRecordDirectory, `${trustedRecordDirectory}-replacement`);
        fs.renameSync(`${trustedRecordDirectory}-old`, trustedRecordDirectory);
        restoredRecordDirectory = true;
      }
      return descriptor;
    };
    try {
      assert.strictEqual(readProtectedRecord(trustedRecord).record.grant, "original");
    } finally {
      fs.lstatSync = originalRecordLstatSync;
      fs.openSync = originalRecordOpenSync;
    }
    assert.strictEqual(
      fs.readFileSync(path.join(`${trustedRecordDirectory}-replacement`, recordName), "utf8"),
      '{"grant":"replacement"}\n'
    );

    const mutableRecord = path.join(trustedRecordDirectory, "mutable-delivery-grant.json");
    fs.writeFileSync(mutableRecord, '{"grant":"original"}\n', { mode: 0o600 });
    const originalMutableRecordReadSync = fs.readSync;
    let mutatedRecordDuringRead = false;
    fs.readSync = (descriptor, ...options) => {
      const count = originalMutableRecordReadSync(descriptor, ...options);
      if (!mutatedRecordDuringRead) {
        fs.writeFileSync(mutableRecord, '{"grant":"modified"}\n', { mode: 0o600 });
        const future = new Date(Date.now() + 2000);
        fs.utimesSync(mutableRecord, future, future);
        mutatedRecordDuringRead = true;
      }
      return count;
    };
    try {
      assert.throws(
        () => readProtectedRecord(mutableRecord),
        /delivery grant state changed while reading/
      );
    } finally {
      fs.readSync = originalMutableRecordReadSync;
    }
    assert(mutatedRecordDuringRead, "the grant must be mutated while its descriptor is being read");
    assert.strictEqual(fs.readFileSync(mutableRecord, "utf8"), '{"grant":"modified"}\n');

    const growingRecord = path.join(trustedRecordDirectory, "growing-delivery-grant.json");
    fs.writeFileSync(growingRecord, '{"grant":"bounded"}\n', { mode: 0o600 });
    const originalGrowingRecordReadSync = fs.readSync;
    let grewRecordDuringRead = false;
    let maximumRecordReadRequest = 0;
    fs.readSync = (descriptor, buffer, offset, length, position) => {
      maximumRecordReadRequest = Math.max(maximumRecordReadRequest, length);
      const count = originalGrowingRecordReadSync(descriptor, buffer, offset, length, position);
      if (!grewRecordDuringRead) {
        fs.appendFileSync(growingRecord, "x".repeat(70 * 1024));
        grewRecordDuringRead = true;
      }
      return count;
    };
    try {
      assert.throws(() => readProtectedRecord(growingRecord), /delivery grant state has an invalid size/);
    } finally {
      fs.readSync = originalGrowingRecordReadSync;
    }
    assert(grewRecordDuringRead, "the grant must grow after its bounded descriptor read begins");
    assert(maximumRecordReadRequest <= 64 * 1024 + 1, "grant reads must remain bounded by the state limit");

    const inspectedGrant = readProtectedRecord(trustedRecord);
    const replacementBeforeConsume = `${trustedRecordDirectory}-replacement`;
    const replacementAfterConsume = `${trustedRecordDirectory}-replacement-after-consume`;
    const originalConsumeLstatSync = fs.lstatSync;
    const originalConsumeUnlinkSync = fs.unlinkSync;
    let exchangedConsumeDirectory = false;
    let restoredConsumeDirectory = false;
    fs.lstatSync = (candidate, ...options) => {
      const details = originalConsumeLstatSync(candidate, ...options);
      if (path.basename(String(candidate)) === recordName && !exchangedConsumeDirectory) {
        fs.renameSync(trustedRecordDirectory, `${trustedRecordDirectory}-consume-old`);
        fs.renameSync(replacementBeforeConsume, trustedRecordDirectory);
        exchangedConsumeDirectory = true;
      }
      return details;
    };
    fs.unlinkSync = (candidate, ...options) => {
      const result = originalConsumeUnlinkSync(candidate, ...options);
      if (String(candidate).endsWith(".remove")
          && exchangedConsumeDirectory && !restoredConsumeDirectory) {
        fs.renameSync(trustedRecordDirectory, replacementAfterConsume);
        fs.renameSync(`${trustedRecordDirectory}-consume-old`, trustedRecordDirectory);
        restoredConsumeDirectory = true;
      }
      return result;
    };
    try {
      removeExactRecord(trustedRecord, inspectedGrant.fileIdentity);
    } finally {
      fs.lstatSync = originalConsumeLstatSync;
      fs.unlinkSync = originalConsumeUnlinkSync;
    }
    assert(!fs.existsSync(trustedRecord), "the originally inspected grant must be consumed");
    assert.strictEqual(
      fs.readFileSync(path.join(replacementAfterConsume, recordName), "utf8"),
      '{"grant":"replacement"}\n'
    );

    const previousInvalidateStateDirectory = process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR;
    const trustedInvalidateDirectory = path.join(temporary, "trusted-invalidate-directory");
    const replacementInvalidateDirectory = path.join(temporary, "replacement-invalidate-directory");
    fs.mkdirSync(trustedInvalidateDirectory, { mode: 0o700 });
    fs.mkdirSync(replacementInvalidateDirectory, { mode: 0o700 });
    process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = trustedInvalidateDirectory;
    const invalidateInput = { session_id: "invalidate-parent-race", agent_id: "main" };
    const invalidateRecordName = `${crypto.createHash("sha256").update("invalidate-parent-race\0main").digest("hex")}.json`;
    fs.writeFileSync(path.join(trustedInvalidateDirectory, invalidateRecordName), '{"grant":"original"}\n', { mode: 0o600 });
    fs.writeFileSync(path.join(replacementInvalidateDirectory, invalidateRecordName), '{"grant":"replacement"}\n', { mode: 0o600 });
    const originalInvalidateUnlinkSync = fs.unlinkSync;
    let exchangedInvalidateDirectory = false;
    fs.unlinkSync = (candidate, ...options) => {
      if (String(candidate).endsWith(".remove") && !exchangedInvalidateDirectory) {
        fs.renameSync(trustedInvalidateDirectory, `${trustedInvalidateDirectory}-old`);
        fs.renameSync(replacementInvalidateDirectory, trustedInvalidateDirectory);
        const result = originalInvalidateUnlinkSync(candidate, ...options);
        fs.renameSync(trustedInvalidateDirectory, `${trustedInvalidateDirectory}-replacement`);
        fs.renameSync(`${trustedInvalidateDirectory}-old`, trustedInvalidateDirectory);
        exchangedInvalidateDirectory = true;
        return result;
      }
      return originalInvalidateUnlinkSync(candidate, ...options);
    };
    try {
      invalidateAgentRecord(invalidateInput);
    } finally {
      fs.unlinkSync = originalInvalidateUnlinkSync;
      if (previousInvalidateStateDirectory === undefined) delete process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR;
      else process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = previousInvalidateStateDirectory;
    }
    assert(exchangedInvalidateDirectory, "the grant parent must be exchanged during invalidation");
    assert(!fs.existsSync(path.join(trustedInvalidateDirectory, invalidateRecordName)), "the original grant must be invalidated");
    assert.strictEqual(
      fs.readFileSync(path.join(`${trustedInvalidateDirectory}-replacement`, invalidateRecordName), "utf8"),
      '{"grant":"replacement"}\n'
    );

    const identityInvalidateDirectory = path.join(temporary, "identity-invalidate-directory");
    const identityInvalidateReplacement = path.join(temporary, "identity-invalidate-replacement");
    fs.mkdirSync(identityInvalidateDirectory, { mode: 0o700 });
    fs.mkdirSync(identityInvalidateReplacement, { mode: 0o700 });
    process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = identityInvalidateDirectory;
    const identityInvalidateInput = { session_id: "invalidate-file-race", agent_id: "main" };
    const identityInvalidateRecordName = `${crypto.createHash("sha256")
      .update("invalidate-file-race\0main").digest("hex")}.json`;
    const identityInvalidateRecord = path.join(identityInvalidateDirectory, identityInvalidateRecordName);
    fs.writeFileSync(identityInvalidateRecord, '{"grant":"original"}\n', { mode: 0o600 });
    fs.writeFileSync(
      path.join(identityInvalidateReplacement, identityInvalidateRecordName),
      '{"grant":"replacement"}\n',
      { mode: 0o600 }
    );
    const originalIdentityInvalidateLstatSync = fs.lstatSync;
    let identityInvalidateRecordChecks = 0;
    let exchangedIdentityInvalidateRecord = false;
    fs.lstatSync = (candidate, ...options) => {
      if (path.basename(String(candidate)) === identityInvalidateRecordName
          && ++identityInvalidateRecordChecks === 2) {
        fs.renameSync(identityInvalidateRecord, `${identityInvalidateRecord}.old`);
        fs.renameSync(
          path.join(identityInvalidateReplacement, identityInvalidateRecordName),
          identityInvalidateRecord
        );
        exchangedIdentityInvalidateRecord = true;
      }
      return originalIdentityInvalidateLstatSync(candidate, ...options);
    };
    try {
      assert.throws(
        () => invalidateAgentRecord(identityInvalidateInput),
        /changed before consumption/
      );
    } finally {
      fs.lstatSync = originalIdentityInvalidateLstatSync;
      if (exchangedIdentityInvalidateRecord) {
        fs.renameSync(
          identityInvalidateRecord,
          path.join(identityInvalidateReplacement, `${identityInvalidateRecordName}.after`)
        );
        fs.renameSync(`${identityInvalidateRecord}.old`, identityInvalidateRecord);
      }
      if (previousInvalidateStateDirectory === undefined) delete process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR;
      else process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = previousInvalidateStateDirectory;
    }
    assert(
      exchangedIdentityInvalidateRecord,
      "the agent grant must be exchanged before identity-bound invalidation"
    );
    assert.strictEqual(fs.readFileSync(identityInvalidateRecord, "utf8"), '{"grant":"original"}\n');
    assert.strictEqual(
      fs.readFileSync(
        path.join(identityInvalidateReplacement, `${identityInvalidateRecordName}.after`),
        "utf8"
      ),
      '{"grant":"replacement"}\n'
    );

    const linkedInvalidateDirectory = path.join(temporary, "linked-invalidate-directory");
    fs.mkdirSync(linkedInvalidateDirectory, { mode: 0o700 });
    process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = linkedInvalidateDirectory;
    const linkedInvalidateInput = { session_id: "invalidate-hard-link", agent_id: "main" };
    const linkedInvalidateRecordName = `${crypto.createHash("sha256")
      .update("invalidate-hard-link\0main").digest("hex")}.json`;
    const linkedInvalidateRecord = path.join(linkedInvalidateDirectory, linkedInvalidateRecordName);
    const linkedInvalidateAlias = `${linkedInvalidateRecord}.alias`;
    fs.writeFileSync(linkedInvalidateRecord, '{"grant":"linked"}\n', { mode: 0o600 });
    fs.linkSync(linkedInvalidateRecord, linkedInvalidateAlias);
    try {
      assert.throws(() => invalidateAgentRecord(linkedInvalidateInput), /hard links/);
      assert(fs.existsSync(linkedInvalidateRecord), "hard-linked invalidation must preserve the grant");
      assert(fs.existsSync(linkedInvalidateAlias), "hard-linked invalidation must preserve the alias");
    } finally {
      if (previousInvalidateStateDirectory === undefined) delete process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR;
      else process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = previousInvalidateStateDirectory;
    }

    const trustedSessionInvalidateDirectory = path.join(temporary, "trusted-session-invalidate-directory");
    const replacementSessionInvalidateDirectory = path.join(temporary, "replacement-session-invalidate-directory");
    fs.mkdirSync(trustedSessionInvalidateDirectory, { mode: 0o700 });
    fs.mkdirSync(replacementSessionInvalidateDirectory, { mode: 0o700 });
    process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = trustedSessionInvalidateDirectory;
    const sessionInvalidateInput = { session_id: "session-invalidate-parent-race", agent_id: "main" };
    const sessionInvalidateHash = crypto.createHash("sha256").update(sessionInvalidateInput.session_id).digest("hex");
    const sessionInvalidateRecordName = "child-grant.json";
    const unrelatedRecordName = "unrelated-grant.json";
    fs.writeFileSync(
      path.join(trustedSessionInvalidateDirectory, sessionInvalidateRecordName),
      `${JSON.stringify({ session_sha256: sessionInvalidateHash })}\n`,
      { mode: 0o600 }
    );
    fs.writeFileSync(
      path.join(trustedSessionInvalidateDirectory, unrelatedRecordName),
      `${JSON.stringify({ session_sha256: "0".repeat(64) })}\n`,
      { mode: 0o600 }
    );
    fs.writeFileSync(
      path.join(replacementSessionInvalidateDirectory, sessionInvalidateRecordName),
      '{"grant":"replacement"}\n',
      { mode: 0o600 }
    );
    const originalSessionInvalidateUnlinkSync = fs.unlinkSync;
    let exchangedSessionInvalidateDirectory = false;
    fs.unlinkSync = (candidate, ...options) => {
      if (String(candidate).endsWith(".remove")
          && !exchangedSessionInvalidateDirectory) {
        fs.renameSync(trustedSessionInvalidateDirectory, `${trustedSessionInvalidateDirectory}-old`);
        fs.renameSync(replacementSessionInvalidateDirectory, trustedSessionInvalidateDirectory);
        const result = originalSessionInvalidateUnlinkSync(candidate, ...options);
        fs.renameSync(trustedSessionInvalidateDirectory, `${trustedSessionInvalidateDirectory}-replacement`);
        fs.renameSync(`${trustedSessionInvalidateDirectory}-old`, trustedSessionInvalidateDirectory);
        exchangedSessionInvalidateDirectory = true;
        return result;
      }
      return originalSessionInvalidateUnlinkSync(candidate, ...options);
    };
    try {
      invalidateSessionRecords(sessionInvalidateInput);
    } finally {
      fs.unlinkSync = originalSessionInvalidateUnlinkSync;
      if (previousInvalidateStateDirectory === undefined) delete process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR;
      else process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = previousInvalidateStateDirectory;
    }
    assert(exchangedSessionInvalidateDirectory, "the session grant parent must be exchanged during invalidation");
    assert(
      !fs.existsSync(path.join(trustedSessionInvalidateDirectory, sessionInvalidateRecordName)),
      "the original session grant must be invalidated"
    );
    assert(fs.existsSync(
      path.join(trustedSessionInvalidateDirectory, unrelatedRecordName)
    ), "another session's grant must remain");
    assert.strictEqual(
      fs.readFileSync(
        path.join(`${trustedSessionInvalidateDirectory}-replacement`, sessionInvalidateRecordName),
        "utf8"
      ),
      '{"grant":"replacement"}\n'
    );

    const identitySessionInvalidateDirectory = path.join(temporary, "identity-session-invalidate-directory");
    const identitySessionInvalidateReplacement = path.join(
      temporary, "identity-session-invalidate-replacement"
    );
    fs.mkdirSync(identitySessionInvalidateDirectory, { mode: 0o700 });
    fs.mkdirSync(identitySessionInvalidateReplacement, { mode: 0o700 });
    process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = identitySessionInvalidateDirectory;
    const identitySessionInvalidateInput = {
      session_id: "session-invalidate-file-race",
      agent_id: "main"
    };
    const identitySessionInvalidateRecordName = `${crypto.createHash("sha256")
      .update("session-invalidate-file-race\0main").digest("hex")}.json`;
    const identitySessionInvalidateRecord = path.join(
      identitySessionInvalidateDirectory, identitySessionInvalidateRecordName
    );
    fs.writeFileSync(identitySessionInvalidateRecord, '{"grant":"original"}\n', { mode: 0o600 });
    fs.writeFileSync(
      path.join(identitySessionInvalidateReplacement, identitySessionInvalidateRecordName),
      '{"grant":"replacement"}\n',
      { mode: 0o600 }
    );
    const originalIdentitySessionInvalidateLstatSync = fs.lstatSync;
    let identitySessionInvalidateRecordChecks = 0;
    let exchangedIdentitySessionInvalidateRecord = false;
    fs.lstatSync = (candidate, ...options) => {
      if (path.basename(String(candidate)) === identitySessionInvalidateRecordName
          && ++identitySessionInvalidateRecordChecks === 2) {
        fs.renameSync(identitySessionInvalidateRecord, `${identitySessionInvalidateRecord}.old`);
        fs.renameSync(
          path.join(identitySessionInvalidateReplacement, identitySessionInvalidateRecordName),
          identitySessionInvalidateRecord
        );
        exchangedIdentitySessionInvalidateRecord = true;
      }
      return originalIdentitySessionInvalidateLstatSync(candidate, ...options);
    };
    try {
      assert.throws(
        () => invalidateSessionRecords(identitySessionInvalidateInput),
        /changed before consumption/
      );
    } finally {
      fs.lstatSync = originalIdentitySessionInvalidateLstatSync;
      if (exchangedIdentitySessionInvalidateRecord) {
        fs.renameSync(
          identitySessionInvalidateRecord,
          path.join(identitySessionInvalidateReplacement, `${identitySessionInvalidateRecordName}.after`)
        );
        fs.renameSync(`${identitySessionInvalidateRecord}.old`, identitySessionInvalidateRecord);
      }
      if (previousInvalidateStateDirectory === undefined) delete process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR;
      else process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = previousInvalidateStateDirectory;
    }
    assert(
      exchangedIdentitySessionInvalidateRecord,
      "the session grant must be exchanged before identity-bound invalidation"
    );
    assert.strictEqual(fs.readFileSync(identitySessionInvalidateRecord, "utf8"), '{"grant":"original"}\n');
    assert.strictEqual(
      fs.readFileSync(
        path.join(identitySessionInvalidateReplacement, `${identitySessionInvalidateRecordName}.after`),
        "utf8"
      ),
      '{"grant":"replacement"}\n'
    );

    const linkedSessionInvalidateDirectory = path.join(temporary, "linked-session-invalidate-directory");
    fs.mkdirSync(linkedSessionInvalidateDirectory, { mode: 0o700 });
    process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = linkedSessionInvalidateDirectory;
    const linkedSessionInvalidateInput = {
      session_id: "session-invalidate-hard-link",
      agent_id: "main"
    };
    const linkedSessionInvalidateRecordName = `${crypto.createHash("sha256")
      .update("session-invalidate-hard-link\0main").digest("hex")}.json`;
    const linkedSessionInvalidateRecord = path.join(
      linkedSessionInvalidateDirectory, linkedSessionInvalidateRecordName
    );
    const linkedSessionInvalidateAlias = `${linkedSessionInvalidateRecord}.alias`;
    fs.writeFileSync(linkedSessionInvalidateRecord, '{"grant":"linked"}\n', { mode: 0o600 });
    fs.linkSync(linkedSessionInvalidateRecord, linkedSessionInvalidateAlias);
    try {
      assert.throws(
        () => invalidateSessionRecords(linkedSessionInvalidateInput),
        /hard links/
      );
      assert(
        fs.existsSync(linkedSessionInvalidateRecord),
        "hard-linked session invalidation must preserve the original grant"
      );
      assert(
        fs.existsSync(linkedSessionInvalidateAlias),
        "hard-linked session invalidation must preserve the alias"
      );
    } finally {
      if (previousInvalidateStateDirectory === undefined) delete process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR;
      else process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = previousInvalidateStateDirectory;
    }

    const writableSessionInvalidateDirectory = path.join(temporary, "writable-session-invalidate-directory");
    fs.mkdirSync(writableSessionInvalidateDirectory, { mode: 0o700 });
    const writableSessionInvalidateRecord = path.join(
      writableSessionInvalidateDirectory, sessionInvalidateRecordName
    );
    fs.writeFileSync(
      writableSessionInvalidateRecord,
      `${JSON.stringify({ session_sha256: sessionInvalidateHash })}\n`,
      { mode: 0o600 }
    );
    fs.chmodSync(writableSessionInvalidateDirectory, 0o777);
    process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = writableSessionInvalidateDirectory;
    try {
      assert.throws(
        () => invalidateSessionRecords(sessionInvalidateInput),
        /directory is writable outside its owner/
      );
      assert(
        fs.existsSync(writableSessionInvalidateRecord),
        "unsafe-parent session invalidation must preserve the grant"
      );
    } finally {
      fs.chmodSync(writableSessionInvalidateDirectory, 0o700);
      if (previousInvalidateStateDirectory === undefined) delete process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR;
      else process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = previousInvalidateStateDirectory;
    }

    const missingSessionInvalidateDirectory = path.join(temporary, "missing-session-invalidate-directory");
    process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = missingSessionInvalidateDirectory;
    try {
      invalidateSessionRecords(sessionInvalidateInput);
      assert(
        !fs.existsSync(missingSessionInvalidateDirectory),
        "session invalidation must not create a missing state directory"
      );
    } finally {
      if (previousInvalidateStateDirectory === undefined) delete process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR;
      else process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = previousInvalidateStateDirectory;
    }

    const writableInvalidateDirectory = path.join(temporary, "writable-invalidate-directory");
    fs.mkdirSync(writableInvalidateDirectory, { mode: 0o700 });
    const writableInvalidateRecord = path.join(writableInvalidateDirectory, invalidateRecordName);
    fs.writeFileSync(writableInvalidateRecord, '{"grant":"preserve"}\n', { mode: 0o600 });
    fs.chmodSync(writableInvalidateDirectory, 0o777);
    process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = writableInvalidateDirectory;
    try {
      assert.throws(() => invalidateAgentRecord(invalidateInput), /directory is writable outside its owner/);
      assert(fs.existsSync(writableInvalidateRecord), "unsafe-parent invalidation must preserve the grant");
    } finally {
      fs.chmodSync(writableInvalidateDirectory, 0o700);
      if (previousInvalidateStateDirectory === undefined) delete process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR;
      else process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = previousInvalidateStateDirectory;
    }

    const previousStateDirectory = process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR;
    const trustedWriteDirectory = path.join(temporary, "trusted-write-directory");
    const replacementWriteDirectory = path.join(temporary, "replacement-write-directory");
    fs.mkdirSync(trustedWriteDirectory, { mode: 0o700 });
    fs.mkdirSync(replacementWriteDirectory, { mode: 0o700 });
    fs.writeFileSync(path.join(replacementWriteDirectory, "sentinel"), "replacement\n", { mode: 0o600 });
    process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = trustedWriteDirectory;
    const writeInput = { session_id: "write-parent-race", agent_id: "main" };
    const writeRecordName = `${crypto.createHash("sha256").update("write-parent-race\0main").digest("hex")}.json`;
    const originalWriteRenameSync = fs.renameSync;
    const originalWriteFchmodSync = fs.fchmodSync;
    const originalWriteChmodSync = fs.chmodSync;
    let exchangedWriteDirectory = false;
    let hardenedWriteDescriptor = false;
    let writePathChmodAttempted = false;
    fs.renameSync = (source, destination, ...options) => {
      if (path.basename(String(destination)) === writeRecordName && !exchangedWriteDirectory) {
        originalWriteRenameSync(trustedWriteDirectory, `${trustedWriteDirectory}-old`);
        originalWriteRenameSync(replacementWriteDirectory, trustedWriteDirectory);
        const result = originalWriteRenameSync(source, destination, ...options);
        originalWriteRenameSync(trustedWriteDirectory, `${trustedWriteDirectory}-replacement`);
        originalWriteRenameSync(`${trustedWriteDirectory}-old`, trustedWriteDirectory);
        exchangedWriteDirectory = true;
        return result;
      }
      return originalWriteRenameSync(source, destination, ...options);
    };
    fs.fchmodSync = (descriptor, mode) => {
      assert.strictEqual(mode, 0o600);
      hardenedWriteDescriptor = true;
      return originalWriteFchmodSync(descriptor, mode);
    };
    fs.chmodSync = (candidate, ...options) => {
      if (path.basename(String(candidate)) === writeRecordName) writePathChmodAttempted = true;
      return originalWriteChmodSync(candidate, ...options);
    };
    try {
      writeRecord(writeInput, { grant: "original" });
    } finally {
      fs.renameSync = originalWriteRenameSync;
      fs.fchmodSync = originalWriteFchmodSync;
      fs.chmodSync = originalWriteChmodSync;
      if (previousStateDirectory === undefined) delete process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR;
      else process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = previousStateDirectory;
    }
    assert(exchangedWriteDirectory, "the grant parent must be exchanged during publication");
    assert(hardenedWriteDescriptor, "grant permissions must be applied to the open temporary descriptor");
    assert(!writePathChmodAttempted, "grant publication must not chmod the published path");
    assert.strictEqual(
      fs.readFileSync(path.join(trustedWriteDirectory, writeRecordName), "utf8"),
      '{"grant":"original"}\n'
    );
    assert.strictEqual(
      fs.readFileSync(path.join(`${trustedWriteDirectory}-replacement`, "sentinel"), "utf8"),
      "replacement\n"
    );

    const temporaryRaceWriteInput = { session_id: "write-temporary-race", agent_id: "main" };
    const temporaryRaceWriteName = `${crypto.createHash("sha256")
      .update("write-temporary-race\0main").digest("hex")}.json`;
    const temporaryRaceWriteTarget = path.join(trustedWriteDirectory, temporaryRaceWriteName);
    process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = trustedWriteDirectory;
    const originalTemporaryRaceWriteLstatSync = fs.lstatSync;
    let temporaryRaceWritePath;
    let temporaryRaceWriteOriginal;
    fs.lstatSync = (candidate, ...options) => {
      const candidateText = String(candidate);
      if (!temporaryRaceWritePath && candidateText.endsWith(".tmp")
          && path.basename(candidateText).startsWith(`${temporaryRaceWriteName}.`)) {
        temporaryRaceWritePath = path.join(trustedWriteDirectory, path.basename(candidateText));
        temporaryRaceWriteOriginal = `${temporaryRaceWritePath}.old`;
        fs.renameSync(candidateText, `${candidateText}.old`);
        fs.writeFileSync(candidateText, '{"grant":"replacement"}\n', { mode: 0o600 });
      }
      return originalTemporaryRaceWriteLstatSync(candidate, ...options);
    };
    try {
      assert.throws(
        () => writeRecord(temporaryRaceWriteInput, { grant: "original" }),
        /temporary file changed before cleanup/
      );
    } finally {
      fs.lstatSync = originalTemporaryRaceWriteLstatSync;
      if (previousStateDirectory === undefined) delete process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR;
      else process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = previousStateDirectory;
    }
    assert(temporaryRaceWritePath, "the grant temporary source must be exchanged");
    assert(!fs.existsSync(temporaryRaceWriteTarget), "an exchanged temporary grant must not be published");
    assert.strictEqual(fs.readFileSync(temporaryRaceWriteOriginal, "utf8"), '{"grant":"original"}\n');
    assert.strictEqual(fs.readFileSync(temporaryRaceWritePath, "utf8"), '{"grant":"replacement"}\n');

    const temporaryHardlinkWriteInput = { session_id: "write-temporary-hard-link", agent_id: "main" };
    const temporaryHardlinkWriteName = `${crypto.createHash("sha256")
      .update("write-temporary-hard-link\0main").digest("hex")}.json`;
    const temporaryHardlinkWriteTarget = path.join(trustedWriteDirectory, temporaryHardlinkWriteName);
    process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = trustedWriteDirectory;
    const originalTemporaryHardlinkWriteOpenSync = fs.openSync;
    const originalTemporaryHardlinkWriteFsyncSync = fs.fsyncSync;
    let temporaryHardlinkWritePath;
    let temporaryHardlinkWriteAlias;
    fs.openSync = (candidate, ...options) => {
      const descriptor = originalTemporaryHardlinkWriteOpenSync(candidate, ...options);
      const candidateText = String(candidate);
      if (!temporaryHardlinkWritePath && candidateText.endsWith(".tmp")
          && path.basename(candidateText).startsWith(`${temporaryHardlinkWriteName}.`)) {
        temporaryHardlinkWritePath = path.join(trustedWriteDirectory, path.basename(candidateText));
        temporaryHardlinkWriteAlias = `${temporaryHardlinkWritePath}.alias`;
      }
      return descriptor;
    };
    fs.fsyncSync = (descriptor) => {
      const result = originalTemporaryHardlinkWriteFsyncSync(descriptor);
      if (temporaryHardlinkWritePath && !fs.existsSync(temporaryHardlinkWriteAlias)) {
        fs.linkSync(temporaryHardlinkWritePath, temporaryHardlinkWriteAlias);
      }
      return result;
    };
    try {
      assert.throws(
        () => writeRecord(temporaryHardlinkWriteInput, { grant: "original" }),
        /temporary file changed before cleanup/
      );
    } finally {
      fs.openSync = originalTemporaryHardlinkWriteOpenSync;
      fs.fsyncSync = originalTemporaryHardlinkWriteFsyncSync;
      if (previousStateDirectory === undefined) delete process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR;
      else process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = previousStateDirectory;
    }
    assert(temporaryHardlinkWritePath, "the temporary grant must be hard-linked during writing");
    assert(!fs.existsSync(temporaryHardlinkWriteTarget), "a hard-linked temporary grant must not be published");
    assert.strictEqual(fs.readFileSync(temporaryHardlinkWritePath, "utf8"), '{"grant":"original"}\n');
    assert.strictEqual(fs.readFileSync(temporaryHardlinkWriteAlias, "utf8"), '{"grant":"original"}\n');

    const publishedRaceWriteInput = { session_id: "write-published-race", agent_id: "main" };
    const publishedRaceWriteName = `${crypto.createHash("sha256")
      .update("write-published-race\0main").digest("hex")}.json`;
    const publishedRaceWriteTarget = path.join(trustedWriteDirectory, publishedRaceWriteName);
    const publishedRaceWriteOriginal = `${publishedRaceWriteTarget}.old`;
    process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = trustedWriteDirectory;
    const originalPublishedRaceWriteRenameSync = fs.renameSync;
    let exchangedPublishedWriteTarget = false;
    fs.renameSync = (source, destination, ...options) => {
      const result = originalPublishedRaceWriteRenameSync(source, destination, ...options);
      if (!exchangedPublishedWriteTarget && path.basename(String(destination)) === publishedRaceWriteName) {
        originalPublishedRaceWriteRenameSync(destination, `${destination}.old`);
        fs.writeFileSync(destination, '{"grant":"replacement"}\n', { mode: 0o600 });
        exchangedPublishedWriteTarget = true;
      }
      return result;
    };
    try {
      assert.throws(
        () => writeRecord(publishedRaceWriteInput, { grant: "original" }),
        /delivery grant state changed during publication/
      );
    } finally {
      fs.renameSync = originalPublishedRaceWriteRenameSync;
      if (previousStateDirectory === undefined) delete process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR;
      else process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = previousStateDirectory;
    }
    assert(exchangedPublishedWriteTarget, "the published grant must be exchanged before final validation");
    assert.strictEqual(fs.readFileSync(publishedRaceWriteOriginal, "utf8"), '{"grant":"original"}\n');
    assert.strictEqual(fs.readFileSync(publishedRaceWriteTarget, "utf8"), '{"grant":"replacement"}\n');

    const hardlinkedWriteInput = { session_id: "write-hard-link", agent_id: "main" };
    const hardlinkedWriteRecordName = `${crypto.createHash("sha256").update("write-hard-link\0main").digest("hex")}.json`;
    const hardlinkedWriteRecord = path.join(trustedWriteDirectory, hardlinkedWriteRecordName);
    const hardlinkedWriteAlias = `${hardlinkedWriteRecord}.alias`;
    fs.writeFileSync(hardlinkedWriteRecord, '{"grant":"preserve"}\n', { mode: 0o600 });
    fs.linkSync(hardlinkedWriteRecord, hardlinkedWriteAlias);
    process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = trustedWriteDirectory;
    try {
      assert.throws(() => writeRecord(hardlinkedWriteInput, { grant: "blocked" }), /hard links/);
      assert.strictEqual(fs.readFileSync(hardlinkedWriteRecord, "utf8"), '{"grant":"preserve"}\n');
      assert.strictEqual(fs.readFileSync(hardlinkedWriteAlias, "utf8"), '{"grant":"preserve"}\n');
    } finally {
      if (previousStateDirectory === undefined) delete process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR;
      else process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = previousStateDirectory;
    }

    const exchangedWriteInput = { session_id: "write-file-race", agent_id: "main" };
    const exchangedWriteRecordName = `${crypto.createHash("sha256").update("write-file-race\0main").digest("hex")}.json`;
    const exchangedWriteRecord = path.join(trustedWriteDirectory, exchangedWriteRecordName);
    const exchangedWriteOriginal = `${exchangedWriteRecord}.old`;
    const exchangedWriteReplacement = `${exchangedWriteRecord}.replacement`;
    fs.writeFileSync(exchangedWriteRecord, '{"grant":"original"}\n', { mode: 0o600 });
    fs.writeFileSync(exchangedWriteReplacement, '{"grant":"replacement"}\n', { mode: 0o600 });
    process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = trustedWriteDirectory;
    const originalWriteLstatSync = fs.lstatSync;
    let writeTargetInspections = 0;
    let exchangedWriteTarget = false;
    fs.lstatSync = (candidate, ...options) => {
      if (path.basename(String(candidate)) === exchangedWriteRecordName) {
        writeTargetInspections += 1;
        if (writeTargetInspections === 2) {
          fs.renameSync(exchangedWriteRecord, exchangedWriteOriginal);
          fs.renameSync(exchangedWriteReplacement, exchangedWriteRecord);
          exchangedWriteTarget = true;
        }
      }
      return originalWriteLstatSync(candidate, ...options);
    };
    try {
      assert.throws(() => writeRecord(exchangedWriteInput, { grant: "blocked" }), /changed before publication/);
    } finally {
      fs.lstatSync = originalWriteLstatSync;
      if (previousStateDirectory === undefined) delete process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR;
      else process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = previousStateDirectory;
    }
    assert(exchangedWriteTarget, "the existing grant must be exchanged before publication");
    assert.strictEqual(fs.readFileSync(exchangedWriteOriginal, "utf8"), '{"grant":"original"}\n');
    assert.strictEqual(fs.readFileSync(exchangedWriteRecord, "utf8"), '{"grant":"replacement"}\n');
    assert(
      !fs.readdirSync(trustedWriteDirectory).some((entry) => entry.startsWith(`${exchangedWriteRecordName}.`) && entry.endsWith(".tmp")),
      "blocked publication must remove its temporary grant"
    );

    const missingWriteDirectory = path.join(temporary, "missing-write-directory");
    process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = missingWriteDirectory;
    try {
      assert.throws(
        () => writeRecord({ session_id: "missing-write-parent", agent_id: "main" }, { grant: "blocked" }),
        /directory cannot be opened safely/
      );
      assert(!fs.existsSync(missingWriteDirectory), "grant writing must not create an unverified state directory");
    } finally {
      if (previousStateDirectory === undefined) delete process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR;
      else process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = previousStateDirectory;
    }
  }
  const token = "a".repeat(64);
  const serviceTokenFile = path.join(temporary, "service.token");
  fs.writeFileSync(serviceTokenFile, `${token}\n`, { mode: 0o600 });
  assert.strictEqual(readProtectedServiceToken(serviceTokenFile), token);
  if (process.platform !== "win32") {
    const linkedToken = path.join(temporary, "linked-service.token");
    fs.symlinkSync(serviceTokenFile, linkedToken);
    assert.throws(() => readProtectedServiceToken(linkedToken), /regular file/);
    const hardlinkedToken = path.join(temporary, "hardlinked-service.token");
    fs.linkSync(serviceTokenFile, hardlinkedToken);
    assert.throws(() => readProtectedServiceToken(serviceTokenFile), /hard links/);
    fs.unlinkSync(hardlinkedToken);

    const stableTokenStats = fs.statSync(serviceTokenFile);
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
      assert.throws(() => readProtectedServiceToken(serviceTokenFile), /changed while reading/);
    } finally {
      fs.fstatSync = originalTokenFstatSync;
    }

    const tokenTarget = path.join(temporary, "service-token-target");
    fs.mkdirSync(tokenTarget, { mode: 0o700 });
    fs.copyFileSync(serviceTokenFile, path.join(tokenTarget, "service.token"));
    const linkedTokenDirectory = path.join(temporary, "linked-service-token-directory");
    fs.symlinkSync(tokenTarget, linkedTokenDirectory, "dir");
    assert.throws(
      () => readProtectedServiceToken(path.join(linkedTokenDirectory, "service.token")),
      /directory cannot be opened safely/
    );

    const writableTokenDirectory = path.join(temporary, "writable-service-token-directory");
    fs.mkdirSync(writableTokenDirectory, { mode: 0o700 });
    const writableToken = path.join(writableTokenDirectory, "service.token");
    fs.copyFileSync(serviceTokenFile, writableToken);
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
      assert.strictEqual(readProtectedServiceToken(exchangedToken), originalToken);
    } finally {
      fs.lstatSync = originalExchangeLstatSync;
      fs.openSync = originalExchangeOpenSync;
    }
    assert.strictEqual(
      fs.readFileSync(path.join(`${trustedTokenDirectory}-replacement`, "service.token"), "utf8"),
      `${replacementToken}\n`
    );
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
  const policyFile = path.join(temporary, "delivery-policy.json");
  fs.writeFileSync(policyFile, JSON.stringify({
    mandatory: true,
    isolated_service: {
      required: true,
      endpoint: `tcp:127.0.0.1:${address.port}`,
      token_file: path.join(temporary, "service.token")
    }
  }), { mode: 0o600 });
  assert.strictEqual(readProtectedDeliveryPolicy(policyFile).mandatory, true);
  if (process.platform !== "win32") {
    const linkedPolicy = path.join(temporary, "linked-policy.json");
    fs.symlinkSync(policyFile, linkedPolicy);
    assert.throws(() => readProtectedDeliveryPolicy(linkedPolicy), /regular file/);
    const hardlinkedPolicy = path.join(temporary, "hardlinked-policy.json");
    fs.linkSync(policyFile, hardlinkedPolicy);
    assert.throws(() => readProtectedDeliveryPolicy(policyFile), /hard links/);
    fs.unlinkSync(hardlinkedPolicy);
    fs.chmodSync(policyFile, 0o644);
    assert.throws(() => readProtectedDeliveryPolicy(policyFile), /permissions/);
    fs.chmodSync(policyFile, 0o600);
  }
  const oversizedPolicy = path.join(temporary, "oversized-policy.json");
  fs.writeFileSync(oversizedPolicy, "{" + " ".repeat(64 * 1024) + "}", { mode: 0o600 });
  assert.throws(() => readProtectedDeliveryPolicy(oversizedPolicy), /invalid size/);
  const originalLstatSync = fs.lstatSync;
  let policyStatsReads = 0;
  fs.lstatSync = (candidate, ...options) => {
    const details = originalLstatSync(candidate, ...options);
    if (path.basename(candidate) === path.basename(policyFile) && ++policyStatsReads === 2) {
      return { ...details, mtimeMs: details.mtimeMs + 1 };
    }
    return details;
  };
  try {
    assert.throws(() => readProtectedDeliveryPolicy(policyFile), /changed while reading/);
  } finally {
    fs.lstatSync = originalLstatSync;
  }
  const stablePolicyStats = fs.statSync(policyFile);
  const relinkedPolicyStats = new Proxy(stablePolicyStats, {
    get(target, property) {
      if (property === "nlink") return target.nlink + 1;
      const value = Reflect.get(target, property);
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
  const originalPolicyFstatSync = fs.fstatSync;
  let policyFstatCalls = 0;
  fs.fstatSync = (...args) => {
    const details = originalPolicyFstatSync(...args);
    if (details.isDirectory()) return details;
    return ++policyFstatCalls === 1 ? stablePolicyStats : relinkedPolicyStats;
  };
  try {
    assert.throws(() => readProtectedDeliveryPolicy(policyFile), /changed while reading/);
  } finally {
    fs.fstatSync = originalPolicyFstatSync;
  }

  if (process.platform !== "win32") {
    const policyTarget = path.join(temporary, "policy-target");
    fs.mkdirSync(policyTarget, { mode: 0o700 });
    const targetPolicy = path.join(policyTarget, "delivery-policy.json");
    fs.copyFileSync(policyFile, targetPolicy);
    fs.chmodSync(targetPolicy, 0o600);
    const linkedPolicyDirectory = path.join(temporary, "linked-policy-directory");
    fs.symlinkSync(policyTarget, linkedPolicyDirectory, "dir");
    assert.throws(
      () => readProtectedDeliveryPolicy(path.join(linkedPolicyDirectory, "delivery-policy.json")),
      /directory cannot be opened safely/
    );

    const writablePolicyDirectory = path.join(temporary, "writable-policy-directory");
    fs.mkdirSync(writablePolicyDirectory, { mode: 0o700 });
    const writablePolicy = path.join(writablePolicyDirectory, "delivery-policy.json");
    fs.copyFileSync(policyFile, writablePolicy);
    fs.chmodSync(writablePolicy, 0o600);
    fs.chmodSync(writablePolicyDirectory, 0o777);
    try {
      assert.throws(
        () => readProtectedDeliveryPolicy(writablePolicy),
        /directory is writable outside its owner/
      );
    } finally {
      fs.chmodSync(writablePolicyDirectory, 0o700);
    }

    const missingPolicy = path.join(temporary, "missing-policy", "config", "delivery-policy.json");
    assert.throws(() => readProtectedDeliveryPolicy(missingPolicy));
    assert(!fs.existsSync(path.dirname(missingPolicy)), "read-only policy checks must not create paths");

    const trustedPolicyDirectory = path.join(temporary, "trusted-policy-directory");
    const replacementPolicyDirectory = path.join(temporary, "replacement-policy-directory");
    fs.mkdirSync(trustedPolicyDirectory, { mode: 0o700 });
    fs.mkdirSync(replacementPolicyDirectory, { mode: 0o700 });
    const exchangedPolicy = path.join(trustedPolicyDirectory, "delivery-policy.json");
    fs.copyFileSync(policyFile, exchangedPolicy);
    fs.chmodSync(exchangedPolicy, 0o600);
    const replacementPolicy = path.join(replacementPolicyDirectory, "delivery-policy.json");
    fs.writeFileSync(replacementPolicy, "{}", { mode: 0o600 });
    const originalExchangeLstatSync = fs.lstatSync;
    let exchangedPolicyDirectory = false;
    fs.lstatSync = (candidate, ...options) => {
      const details = originalExchangeLstatSync(candidate, ...options);
      if (path.basename(candidate) === path.basename(exchangedPolicy) && !exchangedPolicyDirectory) {
        fs.renameSync(trustedPolicyDirectory, `${trustedPolicyDirectory}-old`);
        fs.renameSync(replacementPolicyDirectory, trustedPolicyDirectory);
        exchangedPolicyDirectory = true;
      }
      return details;
    };
    try {
      assert.throws(() => readProtectedDeliveryPolicy(exchangedPolicy), /directory changed while reading/);
    } finally {
      fs.lstatSync = originalExchangeLstatSync;
    }
    assert.strictEqual(fs.readFileSync(exchangedPolicy, "utf8"), "{}");
    assert.match(
      fs.readFileSync(path.join(`${trustedPolicyDirectory}-old`, "delivery-policy.json"), "utf8"),
      /"mandatory":true/
    );

    const traversedPolicyDirectory = path.join(temporary, "traversed-policy-directory");
    const replacementTraversalDirectory = path.join(temporary, "replacement-traversal-directory");
    const traversedPolicyConfig = path.join(traversedPolicyDirectory, "config");
    const replacementTraversalConfig = path.join(replacementTraversalDirectory, "config");
    fs.mkdirSync(traversedPolicyDirectory, { mode: 0o700 });
    fs.mkdirSync(replacementTraversalDirectory, { mode: 0o700 });
    fs.mkdirSync(traversedPolicyConfig, { mode: 0o700 });
    fs.mkdirSync(replacementTraversalConfig, { mode: 0o700 });
    const traversedPolicy = path.join(traversedPolicyConfig, "delivery-policy.json");
    fs.copyFileSync(policyFile, traversedPolicy);
    fs.chmodSync(traversedPolicy, 0o600);
    fs.writeFileSync(
      path.join(replacementTraversalConfig, "delivery-policy.json"),
      "{}",
      { mode: 0o600 }
    );
    const originalTraversalHome = os.homedir;
    const originalTraversalOpenSync = fs.openSync;
    let openedTraversalParent = false;
    let exchangedTraversalParent = false;
    os.homedir = () => temporary;
    fs.openSync = (candidate, ...options) => {
      const basename = path.basename(String(candidate));
      if (basename === path.basename(traversedPolicyDirectory)) openedTraversalParent = true;
      if (basename !== "config" || !openedTraversalParent || exchangedTraversalParent) {
        return originalTraversalOpenSync(candidate, ...options);
      }
      fs.renameSync(traversedPolicyDirectory, `${traversedPolicyDirectory}-old`);
      fs.renameSync(replacementTraversalDirectory, traversedPolicyDirectory);
      try {
        return originalTraversalOpenSync(candidate, ...options);
      } finally {
        fs.renameSync(traversedPolicyDirectory, `${traversedPolicyDirectory}-replacement`);
        fs.renameSync(`${traversedPolicyDirectory}-old`, traversedPolicyDirectory);
        exchangedTraversalParent = true;
      }
    };
    try {
      assert.strictEqual(readProtectedDeliveryPolicy(traversedPolicy).mandatory, true);
    } finally {
      fs.openSync = originalTraversalOpenSync;
      os.homedir = originalTraversalHome;
    }
    assert(exchangedTraversalParent, "the intermediate policy parent must be exchanged during traversal");
    assert.strictEqual(
      fs.readFileSync(
        path.join(`${traversedPolicyDirectory}-replacement`, "config", "delivery-policy.json"),
        "utf8"
      ),
      "{}"
    );
  }

  const environment = { BLUN_LANGUAGE_GUARD_RUNTIME: temporary };
  const previousRuntime = process.env.BLUN_LANGUAGE_GUARD_RUNTIME;
  process.env.BLUN_LANGUAGE_GUARD_RUNTIME = temporary;
  try {
    if (process.platform !== "win32") {
      const previousStateDirectory = process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR;
      const linkedEpochCreateTarget = path.join(temporary, "linked-epoch-create-target");
      const linkedEpochCreateParent = path.join(temporary, "linked-epoch-create-parent");
      fs.mkdirSync(linkedEpochCreateTarget, { mode: 0o700 });
      fs.symlinkSync(linkedEpochCreateTarget, linkedEpochCreateParent, "dir");
      process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = path.join(linkedEpochCreateParent, "claude-hooks");
      await assert.rejects(
        () => beginSessionEpoch({ session_id: "linked-epoch-create-parent", cwd: temporary }),
        /directory cannot be created safely/
      );
      assert(
        !fs.existsSync(path.join(linkedEpochCreateTarget, "claude-hooks")),
        "session startup must not create state through a linked parent"
      );

      const writableEpochCreateParent = path.join(temporary, "writable-epoch-create-parent");
      fs.mkdirSync(writableEpochCreateParent, { mode: 0o700 });
      fs.chmodSync(writableEpochCreateParent, 0o777);
      process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = path.join(writableEpochCreateParent, "claude-hooks");
      try {
        await assert.rejects(
          () => beginSessionEpoch({ session_id: "writable-epoch-create-parent", cwd: temporary }),
          /directory is writable outside its owner/
        );
        assert(
          !fs.existsSync(path.join(writableEpochCreateParent, "claude-hooks")),
          "session startup must not create state below a writable parent"
        );
      } finally {
        fs.chmodSync(writableEpochCreateParent, 0o700);
      }

      const safeEpochCreateParent = path.join(temporary, "safe-epoch-create-parent");
      const safeEpochCreateDirectory = path.join(safeEpochCreateParent, "nested", "claude-hooks");
      fs.mkdirSync(safeEpochCreateParent, { mode: 0o700 });
      process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = safeEpochCreateDirectory;
      const safelyCreatedEpoch = await beginSessionEpoch({
        session_id: "safe-epoch-create-parent",
        cwd: temporary
      });
      assert.match(safelyCreatedEpoch, /^[a-f0-9]{64}$/);
      assert(fs.lstatSync(safeEpochCreateDirectory).isDirectory());
      assert.strictEqual(fs.lstatSync(safeEpochCreateDirectory).mode & 0o777, 0o700);

      const racedEpochCreateParent = path.join(temporary, "raced-epoch-create-parent");
      const racedEpochCreateReplacement = path.join(temporary, "raced-epoch-create-replacement");
      const racedEpochCreateDirectory = path.join(racedEpochCreateParent, "claude-hooks");
      fs.mkdirSync(racedEpochCreateParent, { mode: 0o700 });
      fs.mkdirSync(racedEpochCreateReplacement, { mode: 0o700 });
      process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = racedEpochCreateDirectory;
      const originalEpochCreateMkdirSync = fs.mkdirSync;
      let exchangedEpochCreateParent = false;
      fs.mkdirSync = (candidate, ...options) => {
        const result = originalEpochCreateMkdirSync(candidate, ...options);
        if (!exchangedEpochCreateParent && path.basename(String(candidate)) === "claude-hooks") {
          fs.renameSync(racedEpochCreateParent, `${racedEpochCreateParent}-old`);
          fs.renameSync(racedEpochCreateReplacement, racedEpochCreateParent);
          exchangedEpochCreateParent = true;
        }
        return result;
      };
      try {
        await assert.rejects(
          () => beginSessionEpoch({ session_id: "raced-epoch-create-parent", cwd: temporary }),
          /directory cannot be created safely/
        );
      } finally {
        fs.mkdirSync = originalEpochCreateMkdirSync;
      }
      assert(exchangedEpochCreateParent, "the epoch parent must be exchanged during creation");
      assert(
        !fs.existsSync(path.join(racedEpochCreateParent, "claude-hooks")),
        "an exchanged destination parent must receive no state directory"
      );
      assert(
        fs.lstatSync(path.join(`${racedEpochCreateParent}-old`, "claude-hooks")).isDirectory(),
        "directory creation must stay bound to the held original parent"
      );

      const trustedEpochWriteDirectory = path.join(temporary, "trusted-epoch-write-directory");
      const replacementEpochWriteDirectory = path.join(temporary, "replacement-epoch-write-directory");
      fs.mkdirSync(trustedEpochWriteDirectory, { mode: 0o700 });
      fs.mkdirSync(replacementEpochWriteDirectory, { mode: 0o700 });
      fs.writeFileSync(path.join(replacementEpochWriteDirectory, "sentinel"), "replacement\n", { mode: 0o600 });
      process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = trustedEpochWriteDirectory;
      const epochWriteInput = { session_id: "epoch-write-parent-race", cwd: temporary };
      const epochWriteName = `session-${crypto.createHash("sha256").update(epochWriteInput.session_id).digest("hex")}.epoch`;
      const originalEpochWriteRenameSync = fs.renameSync;
      const originalEpochWriteFchmodSync = fs.fchmodSync;
      const originalEpochWriteChmodSync = fs.chmodSync;
      let exchangedEpochWriteDirectory = false;
      let hardenedEpochWriteDescriptor = false;
      let epochWritePathChmodAttempted = false;
      fs.renameSync = (source, destination, ...options) => {
        if (path.basename(String(destination)) === epochWriteName && !exchangedEpochWriteDirectory) {
          originalEpochWriteRenameSync(trustedEpochWriteDirectory, `${trustedEpochWriteDirectory}-old`);
          originalEpochWriteRenameSync(replacementEpochWriteDirectory, trustedEpochWriteDirectory);
          const result = originalEpochWriteRenameSync(source, destination, ...options);
          originalEpochWriteRenameSync(trustedEpochWriteDirectory, `${trustedEpochWriteDirectory}-replacement`);
          originalEpochWriteRenameSync(`${trustedEpochWriteDirectory}-old`, trustedEpochWriteDirectory);
          exchangedEpochWriteDirectory = true;
          return result;
        }
        return originalEpochWriteRenameSync(source, destination, ...options);
      };
      fs.fchmodSync = (descriptor, mode) => {
        assert.strictEqual(mode, 0o600);
        hardenedEpochWriteDescriptor = true;
        return originalEpochWriteFchmodSync(descriptor, mode);
      };
      fs.chmodSync = (candidate, ...options) => {
        if (path.basename(String(candidate)) === epochWriteName) epochWritePathChmodAttempted = true;
        return originalEpochWriteChmodSync(candidate, ...options);
      };
      let writtenEpoch;
      try {
        writtenEpoch = await beginSessionEpoch(epochWriteInput);
      } finally {
        fs.renameSync = originalEpochWriteRenameSync;
        fs.fchmodSync = originalEpochWriteFchmodSync;
        fs.chmodSync = originalEpochWriteChmodSync;
        if (previousStateDirectory === undefined) delete process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR;
        else process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = previousStateDirectory;
      }
      assert(exchangedEpochWriteDirectory, "the epoch parent must be exchanged during publication");
      assert(
        hardenedEpochWriteDescriptor,
        "epoch permissions must be applied to the open temporary descriptor"
      );
      assert(!epochWritePathChmodAttempted, "epoch publication must not chmod the published path");
      assert.strictEqual(
        fs.readFileSync(path.join(trustedEpochWriteDirectory, epochWriteName), "utf8"),
        `${writtenEpoch}\n`
      );
      assert.strictEqual(
        fs.readFileSync(path.join(`${trustedEpochWriteDirectory}-replacement`, "sentinel"), "utf8"),
        "replacement\n"
      );

      const temporaryRaceEpochInput = { session_id: "epoch-temporary-race", cwd: temporary };
      const temporaryRaceEpochName = `session-${crypto.createHash("sha256")
        .update(temporaryRaceEpochInput.session_id).digest("hex")}.epoch`;
      const temporaryRaceEpochTarget = path.join(trustedEpochWriteDirectory, temporaryRaceEpochName);
      process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = trustedEpochWriteDirectory;
      const originalTemporaryRaceEpochLstatSync = fs.lstatSync;
      let temporaryRaceEpochPath;
      let temporaryRaceEpochOriginal;
      fs.lstatSync = (candidate, ...options) => {
        const candidateText = String(candidate);
        if (!temporaryRaceEpochPath && candidateText.endsWith(".tmp")
            && path.basename(candidateText).startsWith(`${temporaryRaceEpochName}.`)) {
          temporaryRaceEpochPath = path.join(trustedEpochWriteDirectory, path.basename(candidateText));
          temporaryRaceEpochOriginal = `${temporaryRaceEpochPath}.old`;
          fs.renameSync(candidateText, `${candidateText}.old`);
          fs.writeFileSync(candidateText, `${"e".repeat(64)}\n`, { mode: 0o600 });
        }
        return originalTemporaryRaceEpochLstatSync(candidate, ...options);
      };
      try {
        await assert.rejects(
          () => beginSessionEpoch(temporaryRaceEpochInput),
          /temporary file changed before cleanup/
        );
      } finally {
        fs.lstatSync = originalTemporaryRaceEpochLstatSync;
        if (previousStateDirectory === undefined) delete process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR;
        else process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = previousStateDirectory;
      }
      assert(temporaryRaceEpochPath, "the epoch temporary source must be exchanged");
      assert(!fs.existsSync(temporaryRaceEpochTarget), "an exchanged temporary epoch must not be published");
      assert.match(fs.readFileSync(temporaryRaceEpochOriginal, "utf8"), /^[a-f0-9]{64}\n$/);
      assert.strictEqual(fs.readFileSync(temporaryRaceEpochPath, "utf8"), `${"e".repeat(64)}\n`);

      const temporaryHardlinkEpochInput = { session_id: "epoch-temporary-hard-link", cwd: temporary };
      const temporaryHardlinkEpochName = `session-${crypto.createHash("sha256")
        .update(temporaryHardlinkEpochInput.session_id).digest("hex")}.epoch`;
      const temporaryHardlinkEpochTarget = path.join(trustedEpochWriteDirectory, temporaryHardlinkEpochName);
      process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = trustedEpochWriteDirectory;
      const originalTemporaryHardlinkEpochOpenSync = fs.openSync;
      const originalTemporaryHardlinkEpochFsyncSync = fs.fsyncSync;
      let temporaryHardlinkEpochPath;
      let temporaryHardlinkEpochAlias;
      fs.openSync = (candidate, ...options) => {
        const descriptor = originalTemporaryHardlinkEpochOpenSync(candidate, ...options);
        const candidateText = String(candidate);
        if (!temporaryHardlinkEpochPath && candidateText.endsWith(".tmp")
            && path.basename(candidateText).startsWith(`${temporaryHardlinkEpochName}.`)) {
          temporaryHardlinkEpochPath = path.join(trustedEpochWriteDirectory, path.basename(candidateText));
          temporaryHardlinkEpochAlias = `${temporaryHardlinkEpochPath}.alias`;
        }
        return descriptor;
      };
      fs.fsyncSync = (descriptor) => {
        const result = originalTemporaryHardlinkEpochFsyncSync(descriptor);
        if (temporaryHardlinkEpochPath && !fs.existsSync(temporaryHardlinkEpochAlias)) {
          fs.linkSync(temporaryHardlinkEpochPath, temporaryHardlinkEpochAlias);
        }
        return result;
      };
      try {
        await assert.rejects(
          () => beginSessionEpoch(temporaryHardlinkEpochInput),
          /temporary file changed before cleanup/
        );
      } finally {
        fs.openSync = originalTemporaryHardlinkEpochOpenSync;
        fs.fsyncSync = originalTemporaryHardlinkEpochFsyncSync;
        if (previousStateDirectory === undefined) delete process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR;
        else process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = previousStateDirectory;
      }
      assert(temporaryHardlinkEpochPath, "the temporary epoch must be hard-linked during writing");
      assert(!fs.existsSync(temporaryHardlinkEpochTarget), "a hard-linked temporary epoch must not be published");
      assert.match(fs.readFileSync(temporaryHardlinkEpochPath, "utf8"), /^[a-f0-9]{64}\n$/);
      assert.strictEqual(
        fs.readFileSync(temporaryHardlinkEpochAlias, "utf8"),
        fs.readFileSync(temporaryHardlinkEpochPath, "utf8")
      );

      const publishedRaceEpochInput = { session_id: "epoch-published-race", cwd: temporary };
      const publishedRaceEpochName = `session-${crypto.createHash("sha256")
        .update(publishedRaceEpochInput.session_id).digest("hex")}.epoch`;
      const publishedRaceEpochTarget = path.join(trustedEpochWriteDirectory, publishedRaceEpochName);
      const publishedRaceEpochOriginal = `${publishedRaceEpochTarget}.old`;
      process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = trustedEpochWriteDirectory;
      const originalPublishedRaceEpochRenameSync = fs.renameSync;
      let exchangedPublishedEpochTarget = false;
      fs.renameSync = (source, destination, ...options) => {
        const result = originalPublishedRaceEpochRenameSync(source, destination, ...options);
        if (!exchangedPublishedEpochTarget && path.basename(String(destination)) === publishedRaceEpochName) {
          originalPublishedRaceEpochRenameSync(destination, `${destination}.old`);
          fs.writeFileSync(destination, `${"e".repeat(64)}\n`, { mode: 0o600 });
          exchangedPublishedEpochTarget = true;
        }
        return result;
      };
      try {
        await assert.rejects(
          () => beginSessionEpoch(publishedRaceEpochInput),
          /session epoch changed during publication/
        );
      } finally {
        fs.renameSync = originalPublishedRaceEpochRenameSync;
        if (previousStateDirectory === undefined) delete process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR;
        else process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = previousStateDirectory;
      }
      assert(exchangedPublishedEpochTarget, "the published epoch must be exchanged before final validation");
      assert.match(fs.readFileSync(publishedRaceEpochOriginal, "utf8"), /^[a-f0-9]{64}\n$/);
      assert.strictEqual(fs.readFileSync(publishedRaceEpochTarget, "utf8"), `${"e".repeat(64)}\n`);

      const hardlinkedEpochInput = { session_id: "epoch-renew-hard-link", cwd: temporary };
      const hardlinkedEpochName = `session-${crypto.createHash("sha256")
        .update(hardlinkedEpochInput.session_id).digest("hex")}.epoch`;
      const hardlinkedEpochFile = path.join(trustedEpochWriteDirectory, hardlinkedEpochName);
      const hardlinkedEpochAlias = `${hardlinkedEpochFile}.alias`;
      fs.writeFileSync(hardlinkedEpochFile, `${"a".repeat(64)}\n`, { mode: 0o600 });
      fs.linkSync(hardlinkedEpochFile, hardlinkedEpochAlias);
      process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = trustedEpochWriteDirectory;
      try {
        await assert.rejects(() => beginSessionEpoch(hardlinkedEpochInput), /hard links/);
        assert.strictEqual(fs.readFileSync(hardlinkedEpochFile, "utf8"), `${"a".repeat(64)}\n`);
        assert.strictEqual(fs.readFileSync(hardlinkedEpochAlias, "utf8"), `${"a".repeat(64)}\n`);
      } finally {
        if (previousStateDirectory === undefined) delete process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR;
        else process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = previousStateDirectory;
      }

      const removalRaceEpochInput = { session_id: "epoch-renew-removal-race", cwd: temporary };
      const removalRaceEpochName = `session-${crypto.createHash("sha256")
        .update(removalRaceEpochInput.session_id).digest("hex")}.epoch`;
      const removalRaceEpochFile = path.join(trustedEpochWriteDirectory, removalRaceEpochName);
      const removalRaceEpochOriginal = `${removalRaceEpochFile}.old`;
      const removalRaceEpochReplacement = `${removalRaceEpochFile}.replacement`;
      fs.writeFileSync(removalRaceEpochFile, `${"c".repeat(64)}\n`, { mode: 0o600 });
      fs.writeFileSync(removalRaceEpochReplacement, `${"d".repeat(64)}\n`, { mode: 0o600 });
      process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = trustedEpochWriteDirectory;
      const originalEpochRemovalLstatSync = fs.lstatSync;
      let epochRemovalInspections = 0;
      let exchangedEpochRemovalTarget = false;
      fs.lstatSync = (candidate, ...options) => {
        if (path.basename(String(candidate)) === removalRaceEpochName
            && ++epochRemovalInspections === 2) {
          fs.renameSync(removalRaceEpochFile, removalRaceEpochOriginal);
          fs.renameSync(removalRaceEpochReplacement, removalRaceEpochFile);
          exchangedEpochRemovalTarget = true;
        }
        return originalEpochRemovalLstatSync(candidate, ...options);
      };
      try {
        await assert.rejects(
          () => beginSessionEpoch(removalRaceEpochInput),
          /changed before renewal/
        );
      } finally {
        fs.lstatSync = originalEpochRemovalLstatSync;
        if (previousStateDirectory === undefined) delete process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR;
        else process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = previousStateDirectory;
      }
      assert(exchangedEpochRemovalTarget, "the inspected epoch must be exchanged before removal");
      assert.strictEqual(fs.readFileSync(removalRaceEpochOriginal, "utf8"), `${"c".repeat(64)}\n`);
      assert.strictEqual(fs.readFileSync(removalRaceEpochFile, "utf8"), `${"d".repeat(64)}\n`);

      const lateRemovalRaceEpochInput = { session_id: "epoch-renew-late-removal-race", cwd: temporary };
      const lateRemovalRaceEpochName = `session-${crypto.createHash("sha256")
        .update(lateRemovalRaceEpochInput.session_id).digest("hex")}.epoch`;
      const lateRemovalRaceEpochFile = path.join(trustedEpochWriteDirectory, lateRemovalRaceEpochName);
      const lateRemovalRaceEpochOriginal = `${lateRemovalRaceEpochFile}.old`;
      const lateRemovalRaceEpochReplacement = `${lateRemovalRaceEpochFile}.replacement`;
      fs.writeFileSync(lateRemovalRaceEpochFile, `${"e".repeat(64)}\n`, { mode: 0o600 });
      fs.writeFileSync(lateRemovalRaceEpochReplacement, `${"f".repeat(64)}\n`, { mode: 0o600 });
      process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = trustedEpochWriteDirectory;
      const originalLateEpochRemovalRenameSync = fs.renameSync;
      let exchangedLateEpochRemovalTarget = false;
      fs.renameSync = (source, destination, ...options) => {
        if (!exchangedLateEpochRemovalTarget
            && path.basename(String(source)) === lateRemovalRaceEpochName
            && String(destination).endsWith(".remove")) {
          originalLateEpochRemovalRenameSync(lateRemovalRaceEpochFile, lateRemovalRaceEpochOriginal);
          originalLateEpochRemovalRenameSync(lateRemovalRaceEpochReplacement, lateRemovalRaceEpochFile);
          exchangedLateEpochRemovalTarget = true;
        }
        return originalLateEpochRemovalRenameSync(source, destination, ...options);
      };
      try {
        await assert.rejects(
          () => beginSessionEpoch(lateRemovalRaceEpochInput),
          /changed while quarantining/
        );
      } finally {
        fs.renameSync = originalLateEpochRemovalRenameSync;
        if (previousStateDirectory === undefined) delete process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR;
        else process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = previousStateDirectory;
      }
      assert(exchangedLateEpochRemovalTarget, "the epoch must be exchanged at the final removal boundary");
      const quarantinedLateEpoch = fs.readdirSync(trustedEpochWriteDirectory)
        .find((entry) => entry.startsWith(`${lateRemovalRaceEpochName}.`) && entry.endsWith(".remove"));
      assert(quarantinedLateEpoch, "the exchanged epoch must remain quarantined instead of being deleted");
      assert.strictEqual(fs.readFileSync(lateRemovalRaceEpochOriginal, "utf8"), `${"e".repeat(64)}\n`);
      assert.strictEqual(
        fs.readFileSync(path.join(trustedEpochWriteDirectory, quarantinedLateEpoch), "utf8"),
        `${"f".repeat(64)}\n`
      );

      const exchangedEpochInput = { session_id: "epoch-renew-file-race", cwd: temporary };
      const exchangedEpochName = `session-${crypto.createHash("sha256")
        .update(exchangedEpochInput.session_id).digest("hex")}.epoch`;
      const exchangedEpochFile = path.join(trustedEpochWriteDirectory, exchangedEpochName);
      process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = trustedEpochWriteDirectory;
      const originalEpochWriteLstatSync = fs.lstatSync;
      let epochTargetInspections = 0;
      let insertedEpochTarget = false;
      fs.lstatSync = (candidate, ...options) => {
        if (path.basename(String(candidate)) === exchangedEpochName) {
          epochTargetInspections += 1;
          if (epochTargetInspections === 2) {
            fs.writeFileSync(exchangedEpochFile, `${"b".repeat(64)}\n`, { mode: 0o600 });
            insertedEpochTarget = true;
          }
        }
        return originalEpochWriteLstatSync(candidate, ...options);
      };
      try {
        await assert.rejects(
          () => beginSessionEpoch(exchangedEpochInput),
          /changed before publication/
        );
      } finally {
        fs.lstatSync = originalEpochWriteLstatSync;
        if (previousStateDirectory === undefined) delete process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR;
        else process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = previousStateDirectory;
      }
      assert(insertedEpochTarget, "a replacement epoch must appear before publication");
      assert.strictEqual(fs.readFileSync(exchangedEpochFile, "utf8"), `${"b".repeat(64)}\n`);
      assert(
        !fs.readdirSync(trustedEpochWriteDirectory)
          .some((entry) => entry.startsWith(`${exchangedEpochName}.`) && entry.endsWith(".tmp")),
        "blocked epoch publication must remove its temporary marker"
      );

      const writableEpochWriteDirectory = path.join(temporary, "writable-epoch-write-directory");
      fs.mkdirSync(writableEpochWriteDirectory, { mode: 0o777 });
      fs.chmodSync(writableEpochWriteDirectory, 0o777);
      process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = writableEpochWriteDirectory;
      try {
        await assert.rejects(
          () => beginSessionEpoch({ session_id: "unsafe-epoch-write-parent", cwd: temporary }),
          /directory is writable outside its owner/
        );
      } finally {
        fs.chmodSync(writableEpochWriteDirectory, 0o700);
        if (previousStateDirectory === undefined) delete process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR;
        else process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = previousStateDirectory;
      }
    }

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

    if (process.platform !== "win32") {
      const hardlinkedEpoch = `${protectedEpoch}.alias`;
      fs.linkSync(protectedEpoch, hardlinkedEpoch);
      assert.throws(() => readSessionEpoch(protectedInput), /hard links/);
      fs.unlinkSync(hardlinkedEpoch);
    }

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

    if (process.platform !== "win32") {
      const epochName = path.basename(protectedEpoch);
      const previousStateDirectory = process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR;
      const linkedEpochTarget = path.join(temporary, "linked-epoch-target");
      const linkedEpochDirectory = path.join(temporary, "linked-epoch-directory");
      fs.mkdirSync(linkedEpochTarget, { mode: 0o700 });
      fs.writeFileSync(path.join(linkedEpochTarget, epochName), `${"b".repeat(64)}\n`, { mode: 0o600 });
      fs.symlinkSync(linkedEpochTarget, linkedEpochDirectory, "dir");
      process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = linkedEpochDirectory;
      assert.throws(() => readSessionEpoch(protectedInput), /directory cannot be opened safely/);

      const writableEpochDirectory = path.join(temporary, "writable-epoch-directory");
      fs.mkdirSync(writableEpochDirectory, { mode: 0o700 });
      fs.writeFileSync(path.join(writableEpochDirectory, epochName), `${"c".repeat(64)}\n`, { mode: 0o600 });
      fs.chmodSync(writableEpochDirectory, 0o777);
      process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = writableEpochDirectory;
      try {
        assert.throws(() => readSessionEpoch(protectedInput), /directory is writable outside its owner/);
      } finally {
        fs.chmodSync(writableEpochDirectory, 0o700);
      }

      const trustedEpochDirectory = path.join(temporary, "trusted-epoch-directory");
      const replacementEpochDirectory = path.join(temporary, "replacement-epoch-directory");
      fs.mkdirSync(trustedEpochDirectory, { mode: 0o700 });
      fs.mkdirSync(replacementEpochDirectory, { mode: 0o700 });
      const originalEpoch = "d".repeat(64);
      const replacementEpoch = "e".repeat(64);
      fs.writeFileSync(path.join(trustedEpochDirectory, epochName), `${originalEpoch}\n`, { mode: 0o600 });
      fs.writeFileSync(path.join(replacementEpochDirectory, epochName), `${replacementEpoch}\n`, { mode: 0o600 });
      process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = trustedEpochDirectory;
      const originalEpochLstatSync = fs.lstatSync;
      const originalEpochOpenSync = fs.openSync;
      let exchangedEpochDirectory = false;
      let restoredEpochDirectory = false;
      fs.lstatSync = (candidate, ...options) => {
        const details = originalEpochLstatSync(candidate, ...options);
        if (path.basename(String(candidate)) === epochName && !exchangedEpochDirectory) {
          fs.renameSync(trustedEpochDirectory, `${trustedEpochDirectory}-old`);
          fs.renameSync(replacementEpochDirectory, trustedEpochDirectory);
          exchangedEpochDirectory = true;
        }
        return details;
      };
      fs.openSync = (candidate, ...options) => {
        const descriptor = originalEpochOpenSync(candidate, ...options);
        if (path.basename(String(candidate)) === epochName
            && exchangedEpochDirectory && !restoredEpochDirectory) {
          fs.renameSync(trustedEpochDirectory, `${trustedEpochDirectory}-replacement`);
          fs.renameSync(`${trustedEpochDirectory}-old`, trustedEpochDirectory);
          restoredEpochDirectory = true;
        }
        return descriptor;
      };
      try {
        assert.strictEqual(readSessionEpoch(protectedInput).epoch, originalEpoch);
      } finally {
        fs.lstatSync = originalEpochLstatSync;
        fs.openSync = originalEpochOpenSync;
        if (previousStateDirectory === undefined) delete process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR;
        else process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = previousStateDirectory;
      }
      assert.strictEqual(
        fs.readFileSync(path.join(`${trustedEpochDirectory}-replacement`, epochName), "utf8"),
        `${replacementEpoch}\n`
      );

      const mutableEpochInput = { session_id: "epoch-read-mutation", cwd: temporary };
      const mutableEpochName = `session-${crypto.createHash("sha256")
        .update(mutableEpochInput.session_id).digest("hex")}.epoch`;
      const mutableEpochFile = path.join(trustedEpochDirectory, mutableEpochName);
      fs.writeFileSync(mutableEpochFile, `${"a".repeat(64)}\n`, { mode: 0o600 });
      process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = trustedEpochDirectory;
      const originalMutableEpochReadSync = fs.readSync;
      let mutatedEpochDuringRead = false;
      fs.readSync = (descriptor, ...options) => {
        const count = originalMutableEpochReadSync(descriptor, ...options);
        if (!mutatedEpochDuringRead) {
          fs.writeFileSync(mutableEpochFile, `${"b".repeat(64)}\n`, { mode: 0o600 });
          const future = new Date(Date.now() + 2000);
          fs.utimesSync(mutableEpochFile, future, future);
          mutatedEpochDuringRead = true;
        }
        return count;
      };
      try {
        assert.throws(
          () => readSessionEpoch(mutableEpochInput),
          /session epoch changed while reading/
        );
      } finally {
        fs.readSync = originalMutableEpochReadSync;
        if (previousStateDirectory === undefined) delete process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR;
        else process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = previousStateDirectory;
      }
      assert(mutatedEpochDuringRead, "the session epoch must be mutated while its descriptor is being read");
      assert.strictEqual(fs.readFileSync(mutableEpochFile, "utf8"), `${"b".repeat(64)}\n`);

      const growingEpochInput = { session_id: "epoch-read-growth", cwd: temporary };
      const growingEpochName = `session-${crypto.createHash("sha256")
        .update(growingEpochInput.session_id).digest("hex")}.epoch`;
      const growingEpochFile = path.join(trustedEpochDirectory, growingEpochName);
      fs.writeFileSync(growingEpochFile, `${"c".repeat(64)}\n`, { mode: 0o600 });
      process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = trustedEpochDirectory;
      const originalGrowingEpochReadSync = fs.readSync;
      let grewEpochDuringRead = false;
      let maximumEpochReadRequest = 0;
      fs.readSync = (descriptor, buffer, offset, length, position) => {
        maximumEpochReadRequest = Math.max(maximumEpochReadRequest, length);
        const count = originalGrowingEpochReadSync(descriptor, buffer, offset, length, position);
        if (!grewEpochDuringRead) {
          fs.appendFileSync(growingEpochFile, "x".repeat(256));
          grewEpochDuringRead = true;
        }
        return count;
      };
      try {
        assert.throws(() => readSessionEpoch(growingEpochInput), /session epoch has an invalid size/);
      } finally {
        fs.readSync = originalGrowingEpochReadSync;
        if (previousStateDirectory === undefined) delete process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR;
        else process.env.BLUN_LANGUAGE_GUARD_HOOK_STATE_DIR = previousStateDirectory;
      }
      assert(grewEpochDuringRead, "the session epoch must grow after its bounded descriptor read begins");
      assert(maximumEpochReadRequest <= 129, "session epoch reads must remain bounded by the epoch limit");
    }
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

  const encodedNaturalLanguageStop = await runHook("stop", {
    ...common,
    hook_event_name: "Stop",
    last_assistant_message: "&#78;&#97;&#116;&#252;&#114;&#108;&#105;&#99;&#104;",
    stop_hook_active: false
  }, environment);
  assert.strictEqual(JSON.parse(encodedNaturalLanguageStop.stdout).decision, "block");

  const zeroPaddedEncodedNaturalLanguageStop = await runHook("stop", {
    ...common,
    hook_event_name: "Stop",
    last_assistant_message: "&#00000078;&#00000097;&#00000116;&#00000252;&#00000114;&#00000108;&#00000105;&#00000099;&#00000104;",
    stop_hook_active: false
  }, environment);
  assert.strictEqual(JSON.parse(zeroPaddedEncodedNaturalLanguageStop.stdout).decision, "block");

  const semicolonlessEncodedNaturalLanguageSubagentStop = await runHook("stop", {
    ...common,
    agent_id: "child-semicolonless-entity",
    hook_event_name: "SubagentStop",
    last_assistant_message: "&#78 &#97 &#116 &#252 &#114 &#108 &#105 &#99 &#104",
    stop_hook_active: false
  }, environment);
  assert.strictEqual(
    JSON.parse(semicolonlessEncodedNaturalLanguageSubagentStop.stdout).decision,
    "block"
  );

  const c1EncodedNaturalLanguageStop = await runHook("stop", {
    ...common,
    hook_event_name: "Stop",
    last_assistant_message: "&#x8A;&#x9A;",
    stop_hook_active: false
  }, environment);
  assert.strictEqual(JSON.parse(c1EncodedNaturalLanguageStop.stdout).decision, "block");

  const semicolonlessC1NaturalLanguageSubagentStop = await runHook("stop", {
    ...common,
    agent_id: "child-c1-entity",
    hook_event_name: "SubagentStop",
    last_assistant_message: "&#138 &#154",
    stop_hook_active: false
  }, environment);
  assert.strictEqual(
    JSON.parse(semicolonlessC1NaturalLanguageSubagentStop.stdout).decision,
    "block"
  );

  const c1PunctuationStop = await runHook("stop", {
    ...common,
    hook_event_name: "Stop",
    last_assistant_message: "&#128;&#130;&#133;&#145;&#146;&#147;&#148;&#150;&#151;&#153;",
    stop_hook_active: false
  }, environment);
  assert.strictEqual(c1PunctuationStop.stdout, "");

  const encodedEmojiStop = await runHook("stop", {
    ...common,
    hook_event_name: "Stop",
    last_assistant_message: "❤️",
    stop_hook_active: false
  }, environment);
  assert.strictEqual(encodedEmojiStop.stdout, "");

  const encodedKeycapStop = await runHook("stop", {
    ...common,
    hook_event_name: "Stop",
    last_assistant_message: "&#49 &#65039 &#8419",
    stop_hook_active: false
  }, environment);
  assert.strictEqual(encodedKeycapStop.stdout, "");

  const namedPunctuationStop = await runHook("stop", {
    ...common,
    hook_event_name: "Stop",
    last_assistant_message: "&nbsp;&amp;&hellip;&mdash;&copy;",
    stop_hook_active: false
  }, environment);
  assert.strictEqual(namedPunctuationStop.stdout, "");

  const legacyNamedPunctuationStop = await runHook("stop", {
    ...common,
    hook_event_name: "Stop",
    last_assistant_message: "&nbsp&amp&copy&reg&gt&lt&quot&frac12&times",
    stop_hook_active: false
  }, environment);
  assert.strictEqual(legacyNamedPunctuationStop.stdout, "");

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
