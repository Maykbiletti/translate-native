"""Regression: legacy mandatory settings cannot block ordinary conversations."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ConversationScopeTests(unittest.TestCase):
    def environment(self, task="response"):
        env = {k: v for k, v in os.environ.items() if not k.startswith("BLUN_LANGUAGE_GUARD_")}
        env.update(BLUN_LANGUAGE_GUARD_MANDATORY="1", BLUN_LANGUAGE_GUARD_TASK_KIND=task,
                   BLUN_LANGUAGE_GUARD_LANGUAGE="de-DE", BLUN_LANGUAGE_GUARD_SERVICE_ENDPOINT="tcp:127.0.0.1:1")
        return env

    @unittest.skipUnless(shutil.which("node"), "Node required")
    def test_claude_chat_and_subagent_stop_ignore_old_mandatory_and_guard_outage(self):
        for task in ("response", ""):
            for mode, event in (("stop", "Stop"), ("subagent-stop", "SubagentStop")):
                with self.subTest(task=task, mode=mode):
                    result = subprocess.run(["node", "integrations/claude_language_hook.js", mode], cwd=ROOT,
                        input=json.dumps({"hook_event_name": event, "session_id": "fredrik",
                            "agent_id": "child" if mode == "subagent-stop" else None,
                            "stop_hook_active": False, "last_assistant_message": "Haendler sind online."}),
                        env=self.environment(task), capture_output=True, text=True, timeout=5)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, "")

    @unittest.skipUnless(shutil.which("node"), "Node required")
    def test_translation_still_blocks_without_receipt(self):
        result = subprocess.run(["node", "integrations/claude_language_hook.js", "stop"], cwd=ROOT,
            input=json.dumps({"hook_event_name": "Stop", "session_id": "publication",
                "stop_hook_active": False, "last_assistant_message": "Übersetzter Text."}),
            env=self.environment("translation"), capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["decision"], "block")

    @unittest.skipUnless(shutil.which("node"), "Node required")
    def test_unclassified_translation_tool_is_denied(self):
        result = subprocess.run(["node", "integrations/claude_language_hook.js", "pre-tool"], cwd=ROOT,
            input=json.dumps({"hook_event_name": "PreToolUse", "session_id": "chat",
                "tool_name": "mcp__guard__release_translation"}),
            env=self.environment(), capture_output=True, text=True, timeout=5)
        self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    @unittest.skipUnless(shutil.which("node"), "Node required")
    def test_king_ordinary_answer_does_not_connect_or_inject_review(self):
        script = r'''
const assert = require('node:assert/strict');
const { createBlunLanguageGuard } = require('./integrations/adapters/blun-code-language-guard');
const guard = createBlunLanguageGuard({ store: { get() { throw Error('must not load guard'); } },
  getConfig: () => ({ language: 'auto', languageGuardMandatory: true }),
  environment: { BLUN_LANGUAGE_GUARD_MANDATORY: '1' } });
const context = guard.context({ messages: [{ role:'user', content:'Hallo' }] });
assert.equal(context.required, false);
assert.equal(guard.mandatoryInstruction(context), '');
assert.deepEqual(guard.decorateMessages([{role:'user',content:'Hallo'}], context), [{role:'user',content:'Hallo'}]);
(async () => {
 const emitted = [];
 const answer = {answer:'Haendler sind online.'};
 assert.equal(await guard.releaseResult(answer, context, e => emitted.push(e)), answer);
 assert.deepEqual(emitted.map(e => e.type), ['text-delta', 'done']);
 assert.throws(() => guard.context({ messages:[], meta:{ languageGuardTaskKind:'translation',
   languageGuardSourceText:'Hello', languageGuardLanguage:'de-DE' } }));
})().catch(e => { console.error(e); process.exitCode=1; });
'''
        result = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_portable_hooks_allow_chat_without_key_but_reject_translation_downgrade(self):
        for path in ("integrations/pre_output_guard.py", "translate-native/scripts/pre_output_guard.py"):
            for source, allowed in (("", True), ("Complete publication source", False)):
                result = subprocess.run([sys.executable, path], cwd=ROOT,
                    input=json.dumps({"task_kind": "response", "source_text": source,
                        "target_text": "Haendler sind online."}), env=self.environment(),
                    capture_output=True, text=True, timeout=5)
                self.assertEqual(json.loads(result.stdout)["allow"], allowed, result.stdout)

    def test_cli_delivers_plain_chat_without_receipt_or_guard(self):
        result = subprocess.run([sys.executable, "integrations/enforced_delivery.py",
            "--task-kind", "response", "--language", "de-DE"], cwd=ROOT,
            input="  Haendler sind online.\r\n", env=self.environment(), capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "  Haendler sind online.\n")
