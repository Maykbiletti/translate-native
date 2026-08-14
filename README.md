<p align="center">
  <img src=".github/social-preview.png" alt="Translate Native — Meaning in. Native language out." width="100%">
</p>

<div align="center">

<pre>
 ____  _     _   _ _   _
| __ )| |   | | | | \ | |
|  _ \| |   | | | |  \| |
| |_) | |___| |_| | |\  |
|____/|_____|\___/|_| \_|
</pre>

# Translate Native

### Meaning in. Native language out. Release only after proof.

One universal agent skill for translations that sound written—not translated—and preserve every language's native script.

<p>
  <img alt="Tests" src="https://github.com/Maykbiletti/translate-native/actions/workflows/test.yml/badge.svg">
  <img alt="MIT License" src="https://img.shields.io/badge/license-MIT-7C3AED?style=flat-square">
  <img alt="Python 3" src="https://img.shields.io/badge/python-3.10%2B-3776AB?style=flat-square">
  <img alt="Dependencies" src="https://img.shields.io/badge/dependencies-zero-16A34A?style=flat-square">
  <img alt="Languages" src="https://img.shields.io/badge/languages-all-E11D48?style=flat-square">
</p>

**Built by BLUN · Skill + MCP + enforced release gate.**

</div>

---

> **Stop translating strings. Start rewriting meaning in the target language.**

Agents can speak beautifully with users in their own language and still produce stiff, literal copy as soon as the task is called “translation” or arrives inside an i18n file. Translate Native prevents that mode switch.

It reconstructs the meaning, discards the source sentence structure, and writes the message again with native syntax, rhythm, idiom, register, script, punctuation, and locale conventions—without changing the facts.

## One skill. Every human language.

The skill has no language allowlist. It applies equally to:

- Swedish, German, Czech, Spanish, Catalan, Basque, and every other Latin-script language;
- Chinese, Japanese, and Korean;
- Arabic, Persian, Urdu, and Hebrew;
- Greek, Cyrillic, Armenian, and Georgian writing systems;
- Indic, Southeast Asian, Indigenous, minority, endangered, and low-resource languages;
- regional standards, scripts, dialects, honorific systems, and specialist registers.

For uncertain and low-resource varieties, the rule is honesty: verify with community and authoritative sources, or request native review. Never fake fluency.

## What changes

| Ordinary translation mode | Translate Native |
| --- | --- |
| Mirrors source word order | Rebuilds native information flow |
| Chooses dictionary equivalents | Chooses native collocations |
| Produces generic “i18n language” | Writes for the real audience and medium |
| Flattens locale and script choices | Resolves locale, script, dialect, and register |
| Drops accents or romanizes native text | Preserves native spelling, diacritics, alphabets, scripts, and punctuation |
| Silently changes emphasis | Preserves claims, modality, negation, and uncertainty |
| Breaks variables during rewriting | Protects keys, placeholders, URLs, markup, and code |

## Two jobs. One mandatory skill.

Agents no longer need to activate a second orthography skill after translating. `translate-native` now owns both jobs in one self-contained release gate:

1. rewrite the meaning as natural original target-language prose;
2. enforce native spelling, diacritics, alphabets, scripts, punctuation, spacing, and Unicode.

The separate `native-diacritics` skill can still protect ordinary writing outside translation tasks. Inside a translation, its activation is optional because the full orthography contract is built directly into `translate-native`.

The root [`AGENTS.md`](AGENTS.md) tells repository-aware agents to load the combined workflow and treat every model-generated translation as an untrusted draft.

## Version 6: mandatory native output for every agent answer

Version 6 extends the gateway beyond translations. Every user-visible natural-language answer now has a release path:

```text
Agent candidate
      ↓ trusted host classifies the task and supplies the expected locale
      ├── response    → release_response
      └── translation → translate-native skill/plugin → release_translation
      ↓
PASS + purpose-bound receipt → deliver
BLOCK                        → revise or stop
```

`release_response` validates the agent's own answer for Unicode integrity, expected script, native spelling and measurable ASCII folding. It rejects `auto` and `all`: the trusted host must supply the exact language or locale rather than letting the agent choose a convenient label. A German answer such as `Haendler pruefen taeglich die Qualitaet im Buero` blocks; the correctly written `Händler prüfen täglich die Qualität im Büro` can pass.

Translations always take the separate, stricter path. The MCP initialization response tells compatible agents to load the installed `translate-native` skill/plugin before drafting, exposes the same workflow as the MCP prompt `translate-native`, and requires `release_translation` for the complete source-target pair. Translation and response receipts are purpose-bound, so a response receipt cannot authorize a translation.

The portable gateway requires a host-owned `task_kind`:

```json
{
  "task_kind": "response",
  "target_text": "Natürlich können wir das zuverlässig prüfen.",
  "language": "de-DE",
  "attestations": {"nativeness": true, "orthography": true}
}
```

For a translation, use `"task_kind": "translation"`, include the complete `source_text`, and supply all seven translation attestations. The gateway blocks ambiguous task kinds, a translation without source, and any attempt to carry a source through the response route.

This covers every human language and writing system, not only German umlauts. The same contract protects Swedish `å/ä/ö`, Czech `č/ř/š/ž`, Spanish accents and punctuation, Vietnamese tone marks, Greek, Cyrillic, Arabic, Hebrew, Indic scripts, Chinese, Japanese, Korean, and languages not named here. Deterministic checks are intentionally conservative and cannot prove perfect native wording; the native-language workflow and human review remain necessary where consequences are material.

### What “mandatory” really means

An MCP server cannot physically stop an agent that is still allowed to print directly to its terminal, Telegram bridge, API response, or file. Non-bypassable enforcement requires the host to capture the complete candidate output, assign `task_kind` and the expected locale outside the agent's control, call the gateway, verify the purpose-bound receipt, and withhold delivery on every failure. If the agent controls the wrapper, signing key, task classification, source, or delivery channel, the installation is advisory.

Version 6.1 therefore installs [`enforced_delivery.py`](integrations/enforced_delivery.py) as `blun-language-deliver`. In mandatory mode the untrusted agent may emit only this envelope:

```json
{
  "target_text": "Natürlich können wir das zuverlässig prüfen.",
  "release_token": "blg6.…"
}
```

The host—not the envelope—supplies `task_kind`, `language`, `source_text`, and content policy. Any attempt by the agent to include or override those fields blocks delivery. Raw text, invalid JSON, a missing token, an edited target, a wrong locale, an expired token, a response token used for translation, an unavailable guard, or a missing key all produce an empty stdout and a nonzero exit.

The wrapper exposes its trusted classification to the child as read-only process context through `BLUN_LANGUAGE_GUARD_MANDATORY`, `BLUN_LANGUAGE_GUARD_TASK_KIND`, `BLUN_LANGUAGE_GUARD_LANGUAGE`, and `BLUN_LANGUAGE_GUARD_CONTENT_TYPE`. This lets the agent call the correct MCP release tool with the expected locale. Changing those variables inside the child cannot change the host policy used for final verification.

Wrap a non-interactive, JSON-only agent command like this:

```bash
blun-language-deliver \
  --task-kind response \
  --language de-AT \
  -- your-agent-command --json-only
```

For translation the trusted host captures the complete source independently and chooses the stricter route:

```bash
blun-language-deliver \
  --task-kind translation \
  --language sv-SE \
  --source-file /trusted/job/source.txt \
  -- your-agent-command --json-only
```

The same module exposes `guarded_send` and `guarded_send_async` for API, Telegram, queue, and web-response adapters. These functions verify before invoking the supplied sender and never invoke it on failure. The host must remove every alternate sender: an agent with direct Telegram/API credentials or an unwrapped stdout path can still bypass any guard.

For a genuine security boundary, run the MCP signer and delivery verifier under a separate OS identity, container, or remote service. The agent must be unable to read the signing key, modify the gateway, change trusted source files, administer the delivery socket, or call the final channel directly. Same-user installation is strong workflow enforcement, not protection against a hostile process with filesystem access.

### Version 6.12.0: non-mutating portable verification

Version 6.12 closes a trust-root failure in the portable pre-output verifier. Earlier portable and installed-skill hooks used the signer's load-or-create helper: a missing verifier key could therefore create a new signing key before rejecting the current receipt, and a later signer restart could adopt that unrelated key. Both hooks now only read an existing key, require at least 32 bytes, enforce owner-only permissions on POSIX systems, and fail closed without creating directories or files. The explicit `BLUN_LANGUAGE_GUARD_KEY` compatibility path remains available, but no filesystem fallback may initialize or repair signing state.

Regression tests exercise both shipped hook locations, prove that a missing key remains absent, prove that broadly readable keys block on POSIX, and retain the existing exact-receipt success and edited-target rejection controls. Version 6.11's complete context-bound one-time delivery grants remain unchanged.

### Version 6.11.0: complete delivery-context binding

Version 6.11 closes the final context gap between a successful release tool call and Claude's actual `Stop` or `SubagentStop`. The isolated service now signs the canonical source hash, target hash, exact language, task purpose, content type, short-text review flag, delivery channel, Claude session, agent identity, guard version, service boot, expiry, and nonce into every one-time delivery grant. The stop hook must return that complete context when consuming the grant; any changed, missing, stale, copied, or cross-context value blocks delivery.

The Claude hook stores only the canonical source hash, never the complete translation source. The full source remains bound by the original signed release receipt and is independently verified before the delivery grant is issued. A translation grant therefore cannot be relabeled as a normal response, moved to another locale or content policy, or detached from its source context at the last delivery boundary. Version 6.10's deep health probe and existing one-time, target, session, subagent, restart, and replay protections remain unchanged.

### Version 6.10.0: deep MCP health proof

Version 6.10 closes a false-green health gap. The one-minute monitor no longer accepts a signer heartbeat plus MCP initialization and a matching tool list as proof that the language guard can actually execute tools. The isolated service's authenticated health operation now performs an audit-free response release with correct Swedish Unicode, verifies the resulting purpose-bound signature, and proves that a changed target is rejected. The HTTP probe then performs a real MCP `tools/call` using `validate_text` on `Hälsokontrollen är aktiv.` and requires an exact `PASS` result.

No customer text, token, or synthetic canary is written to the audit log. A broken release/signature path, a gateway that merely advertises tools, or a failed tool dispatcher now makes the health monitor block and enter its existing ordered repair and bounded-backoff path. The integration suite runs this complete chain through temporary TCP signer and authenticated HTTP MCP servers without touching installed services.

### Version 6.9.0: transactional safe rollback

Version 6.9 turns the updater's recorded previous revision into an explicit, fail-closed recovery command. `rollback` accepts only an exact 40-character commit recorded by the immediately preceding successful or degraded update, requires the current `HEAD` to match that update, requires a clean worktree, and proves that the target is an available ancestor. It clones the target locally, runs its complete test suite, enforces the saved signed-commit policy when enabled, and changes the active checkout only after every preflight passes. The rolled-back checkout is tested again and already-installed guard and MCP runtimes must restart and pass their live probes; otherwise the updater restores the forward revision.

Claude adds a necessary safety boundary. Anthropic documents `claude plugin update` as updating to the latest plugin and does not document a version-pinned downgrade. Therefore the command never guesses or edits Claude's cache: if the plugin is installed, its enabled, error-free cached version must already equal the rollback target before Git changes. After success, the operating-system scheduler is removed and its policy is preserved as `updater.rollback-paused.json`, so even an older rolled-back installer cannot immediately reinstall the rejected revision. Existing Claude sessions still require `/reload-plugins` or a restart.

### Version 6.8.0: mandatory plugin-cache health

Version 6.8 extends the one-minute health path to the installed Claude plugin cache because the mandatory `Stop` and `SubagentStop` hooks live there. Once the monitor observes an installed `translate-native@blun-language-tools` plugin, it enrolls that cache and checks its enabled state, load errors, and exact version together with the signer and MCP. A stale or unhealthy enrolled cache blocks the overall health result and receives one official `claude plugin update ... --scope user` repair attempt under the same operation lock and exponential backoff as the services.

Enrollment never installs a missing plugin and never reads or edits Claude's private cache layout. The monitor uses the owner-visible Claude executable recorded at installation or updater setup, calls only the documented `plugin list --json` and `plugin update` commands, and verifies the exact version afterward. A successful cache repair still does not claim that an existing session reloaded its hooks: run `/reload-plugins` or start a new session before relying on the new plugin code.

### Version 6.7.1: self-healing guard stack

Version 6.7 adds an independent one-minute health monitor for the two-process Claude path. It verifies both the isolated signer and the complete authenticated MCP `healthz` → `initialize` → `tools/list` path. If the signer fails, it repairs that dependency first and then rechecks the MCP; if only the MCP fails, it restarts only the MCP. Every repair is followed by a complete end-to-end probe before the state may become `recovered`.

The monitor uses systemd on Linux, a LaunchAgent on macOS, and Task Scheduler on Windows. A shared atomic operation lock prevents it from fighting the updater, and each run makes at most one dependency-ordered repair. Version 6.7.1 replaces the ineffective fixed cooldown with persistent exponential backoff: repeated failed repairs wait 1, 2, 5, 15, and then at most 60 minutes, while health probes continue every minute. A skipped probe does not increase the failure count or postpone the next eligible repair, and a successful end-to-end probe resets the backoff immediately. Its state contains only health booleans, timestamps, counters, and repair labels—never source text, target text, receipts, or credentials. Failure remains fail-closed: the monitor never substitutes a local signer or releases pending output while either process is unhealthy.

Fresh Claude installations enable the monitor automatically. Existing automatically updated installations detect the missing health state on their next scheduler wake-up and install it without waiting for the normal update interval:

```bash
python3 installer/blun_language_guard.py health-monitor install
python3 installer/blun_language_guard.py health-monitor status
python3 installer/blun_language_guard.py health-monitor run
```

`health-monitor remove` removes only the monitor schedule; it preserves both services, all secrets, and user configuration.

### Version 6.5: service-owned one-time delivery grants

Version 6.5 removes the local Claude hook record as a trust decision. After `release_response` or `release_translation`, the `PostToolUse` hook sends the complete receipt context to the isolated service. A valid receipt is exchanged for a short-lived signed delivery grant bound to the exact target hash, Claude session, agent or subagent, guard version, service boot, purpose, locale, and expiry.

The owner-only hook file contains the opaque delivery grant, canonical source and target hashes, signed context labels, and authorization time—never the source or target prose. At `Stop` or `SubagentStop`, the hook deletes the local record before asking the isolated service to consume the grant for the actual `last_assistant_message` and exact recorded context. The service accepts each nonce exactly once. Copying a consumed record, forging or relabeling a local record, changing the final response, moving a grant to another session or subagent, restarting the signer, or crossing a version boundary now fails closed.

This strengthens the ordinary same-user installation without overstating it. A process that can read the service authentication token, replace managed hooks, or reach the final delivery channel can still bypass workflow enforcement. Use a separate OS identity, container, remote signer, and host-owned delivery credentials for a hostile-process boundary.

### Version 6.4: Claude plugin and mandatory final-response hooks

The repository is now both a Claude Code plugin and a Claude plugin marketplace. The plugin bundles the `translate-native` skill, connects to the persistent HTTP MCP, injects mandatory policy at session start, observes successful release-tool calls, and applies `Stop` plus `SubagentStop` hooks to the actual final response.

The `PostToolUse` hook does not trust the MCP result by appearance. It sends the receipt, exact target, complete source for translations, purpose, locale, and content policy to the isolated verifier. Version 6.5 exchanges a valid result for a service-owned one-time delivery grant rather than trusting the local hook record itself. A missing, stale, replayed, wrong-purpose, or post-release-edited result prevents Claude from stopping and tells it to run the proper release path again.

Install the persistent runtime first, then the marketplace plugin:

```bash
python3 installer/blun_language_guard.py install --target claude
claude plugin marketplace add Maykbiletti/translate-native --scope user
claude plugin install translate-native@blun-language-tools --scope user
```

In Claude's `/plugin` interface, open **Marketplaces → blun-language-tools → Enable auto-update**. Claude then refreshes the marketplace and installed plugin at startup. A changed plugin is activated by `/reload-plugins` or the next session; the independently installed HTTP service continues running during that change.

The plugin's checked-in components are:

- [`.claude-plugin/plugin.json`](.claude-plugin/plugin.json): versioned plugin manifest;
- [`.claude-plugin/marketplace.json`](.claude-plugin/marketplace.json): GitHub marketplace catalog;
- [`.mcp.json`](.mcp.json): plugin-scoped persistent MCP connection;
- [`hooks/hooks.json`](hooks/hooks.json): session, release-tool, main-agent, and subagent hooks;
- [`claude_language_hook.js`](integrations/claude_language_hook.js): cross-platform, zero-package verification state machine.

This closes accidental omission and catches an agent that validates one draft but returns another. A forged hook-state file alone no longer passes because the isolated service must consume its signed grant. It is still not a hostile-process boundary: same-user code may be able to read service credentials or disable an unmanaged plugin. For organization-wide enforcement, force-enable the plugin and managed hooks, remove direct delivery credentials from the agent, or place a buffering BLUN host in front of rendered output.

### Version 6.3: persistent Claude MCP

Claude Code no longer needs to keep this guard alive as a child `stdio` process. The installer registers an authenticated, user-scoped Streamable HTTP server at `http://127.0.0.1:47632/mcp`. The endpoint is stateless: every tool call is a separate request, so a disconnected client pipe cannot kill the server or erase its tools. The operating system keeps the process alive and restarts it after a failure. This follows Claude Code's documented [user-scoped HTTP MCP configuration](https://code.claude.com/docs/en/mcp) and the MCP specification's [Streamable HTTP transport](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports).

```bash
python3 installer/blun_language_guard.py install --target claude
python3 installer/blun_language_guard.py mcp-service status
python3 installer/blun_language_guard.py doctor
```

Installation safely updates the top-level user entry in `~/.claude.json`, preserves unrelated settings and MCP servers, creates `~/.claude.json.bak`, and removes stale same-name local entries stored under individual projects in that file. This matters because Claude's local and project scopes take precedence over user scope; an old project-specific `stdio` definition can otherwise make the repaired server appear unreliable only in certain repositories. Checked-in `.mcp.json` files remain project-owned and must not define another server with the same name.

The exact generated shape is also available as [`claude-http.example.json`](mcp-server/claude-http.example.json) for inspection. Let the installer write the machine-specific absolute helper path instead of copying the example by hand.

The Claude entry uses [`mcp_auth_headers.py`](integrations/mcp_auth_headers.py) as `headersHelper`. Claude runs that helper again at connection time and after an authentication retry, so the bearer token remains in an owner-only file instead of being copied into configuration. The HTTP server binds only to loopback, validates browser origins, requires authentication, rejects oversized or invalid requests without exiting, accepts UTF-8 BOM input, and exposes an authenticated `/healthz` probe covering the complete path to the isolated guard service.

The persistent transport improves availability; it does not turn MCP instructions into a security boundary. The trusted host must still intercept output and fail closed. If the HTTP service or isolated signer is unavailable, delivery remains blocked rather than falling back to local signing or raw text.

### Version 6.2: isolated runtime enforcement

Version 6.2 moves signing and final verification into [`guard_service.py`](integrations/guard_service.py), a dedicated loopback service. The MCP process and host adapters receive only a service endpoint and an authentication token; the untrusted agent child receives neither the signing key nor the service token. The service accepts UTF-8 with or without a BOM, signs releases, verifies the exact envelope at delivery time, and appends a content-free audit record containing only hashes, route metadata, guard version, and finding codes.

The installer now creates and starts the service automatically through systemd user services on Linux, a LaunchAgent on macOS, or Task Scheduler on Windows:

```bash
python3 installer/blun_language_guard.py install
python3 installer/blun_language_guard.py service status
python3 installer/blun_language_guard.py doctor
```

Use `service start` or `service stop` for explicit lifecycle control. `install --no-service-autostart` exists for packaging and tests, but a production host must not deliver model output in that state. A per-user service prevents accidental key exposure to child processes; resisting a hostile same-user agent still requires a separate service account, container, or remote signer with filesystem and channel credentials denied to the agent.

Two host adapters are included:

- [`node-language-guard.js`](integrations/adapters/node-language-guard.js) provides strict routing, envelope parsing, isolated verification, and verify-before-send Telegram delivery for Node.js hosts.
- [`blun-code-language-guard.js`](integrations/adapters/blun-code-language-guard.js) migrates the installed BLUN MCP entry into BLUN Code's encrypted MCP store, buffers model text so an unsigned draft cannot leak through streaming, and releases only the verified target.

The trusted router uses structured job metadata, never the agent's claim. A source-bearing or explicitly translated job takes `release_translation`; an ordinary reply takes `release_response`. Contradictory metadata, `auto`, `all`, missing translation source, raw prose, unknown envelope fields, an invalid receipt, an unavailable service, or a sender invocation before verification all block.

Free-form text alone cannot provide non-bypassable task classification. A host that offers translation through chat must set `languageGuardTaskKind: translation`, capture the complete source independently as `languageGuardSourceText`, and set the exact target language. If that metadata is absent, the BLUN adapter instructs the agent not to perform a translation through the response route. See [`BLUN_CODE_INTEGRATION.md`](docs/BLUN_CODE_INTEGRATION.md) for the runtime contract and residual limits.

## Version 5: BLUN Language Gateway

Version 5 makes the host—not the agent—the final authority. Skills and MCP tools can be forgotten or skipped. A mandatory gateway intercepts the candidate output and releases it only after validation produces a signed receipt for the exact source, target, and locale.

```text
User → Agent → intercepted candidate → BLUN Language Gateway
                                      ├── PASS + receipt → release
                                      └── BLOCK          → revise or stop
```

Use the portable gateway with JSON on stdin:

```bash
python3 integrations/language_gateway.py < release-request.json
```

Strong enforcement requires the gateway and signing key to run outside the agent's writable sandbox, ideally as a separate OS user, container, or remote service. If the agent can replace the gateway or read its key, the installation is advisory—not non-bypassable. CLI adapters must capture final output before it is printed; API gateways must withhold the HTTP response; CI must require the check before merge and deployment.

### Automatic safe updates

Enable the operating-system scheduler once:

```bash
python3 installer/blun_language_guard.py auto-update enable --interval-hours 24
python3 installer/blun_language_guard.py auto-update status
```

Linux uses a user-level systemd timer, macOS a LaunchAgent, and Windows Task Scheduler. Each scheduled wake-up checks whether the configured interval is due. A candidate checkout is tested before installation, the update is fast-forward-only, post-update tests run again, and the previous revision is retained for rollback. When Claude is installed, an update also installs or refreshes the persistent HTTP MCP, its dynamic-header helper, its autostart service, and the user-scoped Claude entry before marking the runtime update successful. Security-sensitive deployments can require trusted Git commit signatures:

```bash
python3 installer/blun_language_guard.py auto-update enable --require-signed-commits
```

Version 6.6 also coordinates Claude's plugin cache. When automatic updates are enabled, the owner-visible Claude executable path is recorded so an OS scheduler with a smaller `PATH` invokes the same CLI. If `translate-native@blun-language-tools` is already installed at user scope, the updater runs Claude's official non-interactive `plugin update` command and then verifies the exact installed version through `plugin list --json`. A missing plugin is never installed without consent. A plugin failure leaves the already-tested runtime and fail-closed MCP active, records a `degraded` updater state, returns nonzero, and retries on the next scheduled wake-up instead of waiting for the normal interval. Version 6.7 uses the same degraded retry path for a health-monitor installation failure and shares an operation lock between updates and repairs. Version 6.8 enrolls an observed installed cache in the one-minute monitor, so disabling it, load errors, or later version drift can no longer leave the services green while the mandatory hooks are unhealthy. A successful cache update still requires `/reload-plugins` or a new Claude session because active sessions retain their previously loaded hook paths.

To return to the exact revision saved by the last update, synchronize an installed Claude plugin to that target version first, make sure the checkout is clean, and run:

```bash
python3 installer/blun_language_guard.py rollback
```

Use `--require-signed-commits` to require a trusted signature even when the saved updater policy did not. The command never selects an arbitrary revision, never downgrades a plugin through undocumented cache manipulation, and never overwrites local changes. A successful rollback pauses scheduled updates; inspect the result, then run an explicit `update` followed by `auto-update enable` when you intentionally want to resume the forward line.

The authoritative plugin version lives only in `.claude-plugin/plugin.json`; the marketplace entry deliberately omits a duplicate version field. Claude uses the manifest version as its cache key, avoiding two version declarations that can drift apart.

Third-party marketplace auto-update is disabled by default in Claude. Users may enable it in the marketplace UI as an additional startup check; the operating-system updater no longer depends on that optional setting. Other platform-native plugin stores remain controlled by their host platform.

BLUN Code is supported explicitly. Installation creates the BLUN skill symlink and safely merges `blun-language-guard` into `~/.blun/mcp.json`, preserving the other MCP servers and writing `mcp.json.bak` before a change. BLUN Code must be restarted once after initial installation; subsequent repository updates are visible through the symlink automatically.

### Production regressions fixed in Version 5

- Page-sized releases are covered by a regression test above 7,000 characters with a one-second local execution budget.
- MCP JSON input accepts the UTF-8 BOM frequently emitted by Windows tooling.
- Long Swedish, German, Spanish, Czech, and Catalan targets receive a language-character profile check that catches wholesale ASCII folding even when no word appears in a small substitution list.
- The Swedish BLUN ASCII-folding regression was found by **Angel** and is retained under her name in the permanent evaluation corpus.

### Version 5.1: short copy and transport-safe identity

Titles, meta descriptions, and UI strings below 200 characters never receive a deterministic `PASS` from character profiles or substitution lists. The gate always returns `REVIEW_REQUIRED` until an independent native review sets `short_text_reviewed: true`; that decision, together with `content_type`, is cryptographically bound into the receipt. This remains true even when the short text still contains some native characters, preventing one surviving `å`, `ä`, or `ö` from hiding another destroyed word.

Text identity now distinguishes transport differences from corruption. Canonical hashes remove a leading UTF-8 BOM, normalize CRLF and lone CR to LF, and normalize Unicode to NFC. Mojibake and actual character changes remain different and invalidate the receipt.

Both portable commands are now present inside the installable skill as well as the repository integration layer:

- `translate-native/scripts/language_gateway.py`
- `translate-native/scripts/pre_output_guard.py`

Symlink installations receive these files with the next automatic update; no manual copying is required.

### Version 5.2: measurable evidence beats self-attestation

Version 5.1 correctly raised a short-copy gate but incorrectly allowed the same caller to declare both `content_type` and `short_text_reviewed`. Those values are not independent evidence and are no longer a security boundary.

Version 5.2 measures conventional ASCII-folding pressure for German, Swedish, Danish, and Norwegian on every target, regardless of length or declared content type. Measurable folding findings cannot be overridden by `short_text_reviewed: true` or `content_type: prose`.

Version 5.2.1 removes German `ss` from that measurement. Unlike `ae`, `oe`, and `ue`, `ss` is frequently correct native spelling (`wissen`, `dass`, `interessiert`) and cannot be classified as a `ß` replacement without lexical and locale context. The guard deliberately leaves cases such as `Grösse` to a future `de-DE`/`de-AT`/`de-CH` dictionary-aware check instead of creating a broad false positive.

### Version 5.3: quantity is part of integrity

`translation_guard.py` now measures linguistic volume in addition to tags and protected tokens. It compares total Unicode letter/number volume, non-empty linguistic segment counts, and aligned segment coverage for text, JSON/ARB, HTML, XML/XLIFF/Android resources, PO, Apple strings, and subtitles. Script-aware thresholds allow naturally compact CJK translations while blocking major omissions such as a 64-unit target derived from a 224-unit source.

A successful deterministic guard now says exactly what it proves: measurable structure, protected tokens, linguistic volume, and Unicode integrity. It explicitly does **not** prove semantic fidelity, true completeness, or native quality. A literal translation can have the right length and still fail the native-language gate.

Version 5.3.1 also makes the volume check unconditional inside the mandatory MCP `release_translation` path. Format detection is derived from the source syntax rather than a caller-supplied checkbox, so truthful-looking attestations cannot release a measurably truncated target. Structured CLI validation remains required for exact tag, key, placeholder, and technical-value integrity.

Version 5.3.2 makes every MCP dependency resolve relative to the installed script itself. The server no longer assumes the repository's `translate-native/scripts` directory layout, and an isolated-install regression test starts the copied server from the exact flat `scripts/` layout used by installed skills.

Version 5.3.3 blocks substantial unchanged targets. When source and target remain identical after transport-only BOM, newline, surrounding-whitespace, and NFC normalization, a source of at least 200 characters fails both the CLI and mandatory MCP release with `source-target-identical`. Short shared terms such as `BLUN King` and `E-Mail` remain valid. The comparison covers the complete input—not only `<main>` or another convenient content subtree.

Version 5.3.4 closes the structured-file segment gap. Human-language values in JSON/ARB, HTML, XML/XLIFF/Android resources, PO, Apple strings, subtitles, and plain text are compared independently. An unchanged segment of at least 24 linguistic units now fails the CLI and mandatory MCP release, even when every other value was translated. JSON keys are aligned independently of property order; other structured formats use cross-target segment identity, so swapping two unchanged HTML or XML values cannot bypass the gate. Copyright and rights notices receive no automatic fixed-content exemption: an unchanged copyright-marked segment is blocked even below the normal segment threshold. Short product names such as `BLUN King` remain valid. A legitimate fixed legal line requires explicit human handling outside the automatic release gate; it is never silently passed.

Read the diagnostic text, not only the exit code. A blocked identity comparison must explicitly report `target is unchanged` or `linguistic segment is unchanged`; JSON findings name the exact path such as `$.hinweis`. The structural translation guard reserves exit code `1` for evaluated content that was blocked and returns `2` when an input file cannot be read. A missing path reports `cannot read file` and is not evidence that the identity detector fired. Tests assert both the expected result and the expected reason.

Other scripts cannot always be reconstructed from stripped ASCII without a dictionary or native model. The guard reports only what it can measure and never claims that this heuristic proves correct spelling. Strong independence still requires the external Language Gateway and reviewer to run outside the releasing agent's authority.

## Version 4 foundation: signed release receipts

Version 4 turns the executable MCP gate into a signed, independently verifiable release system. The skill remains responsible for meaning, native rewriting, locale fit, and orthography. The server blocks deterministic defects and issues a cryptographic receipt bound to the exact source, target, locale, version, issue time, and expiry.

```text
Translation request
        ↓
translate-native skill
        ↓
Native pass → fidelity pass → integrity pass
        ↓
release_translation MCP tool
        ↓
BLOCK → revise and repeat
PASS  → signed receipt → verify receipt → deliver
```

The gate checks UTF-8/Unicode integrity, NFC normalization, script identity, balanced bidirectional isolates, dangerous overrides, frequent ASCII substitutions, optional terminology glossaries, and all seven mandatory attestations. Edited, expired, forged, wrong-locale, or wrong-version receipts fail verification.

### Install, update, and diagnose

```bash
python3 installer/blun_language_guard.py install
python3 installer/blun_language_guard.py doctor
python3 installer/blun_language_guard.py update
```

Installation uses atomic symlinks for Codex and Claude Code and refuses to overwrite existing non-symlink skill folders. It installs `~/.local/bin/blun-language-deliver`, the isolated service command, owner-only key and service-token files, autostart configuration, a content-free audit path, and a fail-closed policy; writes a mergeable MCP snippet without overwriting unrelated host configuration; and merges the BLUN MCP entry safely. Updates are cloned and tested before the active checkout is fast-forwarded, then the service is restarted and health-checked; a failed restart rolls the checkout back. `doctor` checks the delivery command, service health, secret permissions, mandatory policy, test suite, live MCP tools, signed receipts, and updater heartbeat.

The portable fail-closed hook is [`pre_output_guard.py`](integrations/pre_output_guard.py). It accepts `task_kind`, target, locale, receipt, and the complete source for translations as JSON on stdin and exits nonzero when verification fails. It only reads an existing owner-only verification key and never creates or repairs signing state. Host-specific adapters must pass the candidate output into this contract; a host hook that exposes no candidate text cannot enforce output validation.

### Supported structured formats

- JSON and ARB;
- HTML including linguistic metadata and JSON-LD linguistic fields while protecting schema, URLs, types, code, and placeholders;
- XML, Android resources, and structurally equivalent XLIFF documents;
- PO/POT catalogs;
- Apple `.strings`;
- SRT, VTT, and ASS subtitle timing;
- ICU placeholders and plural/select contracts inside supported containers.

See [`PREMORTEM.md`](docs/PREMORTEM.md) for the failure modes, mitigations, and proof required before calling this system production-ready.

No deterministic linter can prove that prose is genuinely native. That is why the MCP server supplements the skill's native-language judgment instead of pretending to replace it.

### Start the MCP server

For Claude Code, use the persistent runtime shown in Version 6.3 together with the current Version 6.12.0 plugin. The HTTP MCP remains available in every project through user scope, while the plugin adds the mandatory lifecycle hooks and the operating-system monitor repairs its service path and enrolled plugin cache. Check the runtime at any time with:

```bash
python3 installer/blun_language_guard.py mcp-service status
claude mcp get blun-language-guard
```

For other clients, the zero-dependency `stdio` transport remains available as a compatibility fallback:

```bash
python3 translate-native/scripts/blun_language_guard.py serve
```

Copy [`mcp-config.example.json`](mcp-server/mcp-config.example.json), replace the absolute path, and merge the `blun-language-guard` entry into the MCP configuration used by that agent CLI. Do not use the `stdio` fallback for Claude after installing Version 6.3; a higher-precedence project or local entry with the same name can shadow the persistent user server.

Then copy the rules in [`AGENT_RULES.md`](integrations/AGENT_RULES.md) into the host's always-on instruction file:

- Codex: repository or global `AGENTS.md`;
- Claude Code: `CLAUDE.md`;
- another MCP-compatible agent: its equivalent persistent instruction file.

This combination matters. A skill can fail to trigger, and an MCP tool can remain unused. The persistent rule requires `release_response` for ordinary answers and both the `translate-native` workflow and `release_translation` for translations. The trusted host gateway remains the final enforcement boundary.

### Validate from the terminal

The same engine can be used in hooks, CI, wrappers, or pre-publication scripts:

```bash
python3 translate-native/scripts/blun_language_guard.py validate --language sv-SE target.txt
```

Exit code `0` means the deterministic checks passed. Exit code `1` means delivery must stop. Semantic and native-quality review remains mandatory even after exit code `0`.

## Native examples

| Target | Native result |
| --- | --- |
| Swedish | `Om du redan har betalat behöver du inte göra något mer.` |
| Simplified Chinese | `你确认前，我们不会分享任何内容。` |
| Catalan | `No es compartirà res fins que ho confirmis.` |
| Basque | `Dagoeneko ordaindu baduzu, ez duzu beste ezer egin behar.` |
| Czech | `Pokud jste již zaplatili, nemusíte nic dalšího dělat.` |
| Spanish | `Si ya has pagado, no tienes que hacer nada más.` |

These are examples, not the boundary of the skill.

## Grammatical is not native

The skill now explicitly rejects agent-written copy that is grammatically correct but still sounds translated. Agents must inspect collocations, parallel structure, paragraph flow, register, unnecessary source-language borrowings, and vague AI filler—not just spelling and syntax.

The Swedish BLUN regression case captures the difference:

| Agent wording | Native review |
| --- | --- |
| `desktop- och mobilprogramvara` | `programvara för datorer och mobila enheter` |
| `fördela uppgiften på ett smart sätt` | `fördela arbetet mellan de modeller som passar bäst för uppgiften` |
| `fördela uppgiften till den modell som passar bäst` | `automatiskt välja den modell som passar bäst för uppgiften` |

All three agent formulations are understandable. They still fail because a native editor would recast them for publication. The complete candidates, defect analyses, native rewrites, and language-independent review procedure live in [`translationese-review.md`](translate-native/references/translationese-review.md).

## The translation contract

Every result passes seven gates:

1. **Meaning** — claims, relationships, conditions, and implications remain equivalent.
2. **Completeness** — nothing is lost, duplicated, or invented.
3. **Precision** — negation, modality, quantities, entities, and terminology remain exact.
4. **Nativeness** — calques, source syntax, and translationese are removed.
5. **Fit** — locale, script, register, audience, tone, and medium are right.
6. **Integrity** — placeholders, keys, links, markup, code, and structure survive intact.
7. **Orthography** — spelling, diacritics, punctuation, spacing, and Unicode are native.

Version 2 deliberately separates judgment into two passes: a target-only native edit that cannot lean on the source wording, followed by a source-aware fidelity audit that accounts for every claim and constraint. Publication-grade, long-form, and uncertain work can be routed through an independent defect review before release.

## i18n without i18n language

JSON, YAML, XML, PO, ARB, ICU MessageFormat, Android resources, Apple strings, Markdown, and HTML are containers. They do not excuse robotic prose.

```json
{
  "welcome": "Ongi etorri, {name}!",
  "upload": "Kargatu {{count}} fitxategi <strong>{project}</strong> proiektura.",
  "help": "Informazio gehiago: https://example.com/help"
}
```

The key structure stays fixed. The Basque values read naturally. Every placeholder, URL, and HTML tag remains protected.

## Install

Clone the repository:

```bash
git clone https://github.com/Maykbiletti/translate-native.git
```

Copy the `translate-native` directory into the skill directory used by your compatible agent platform, or load its [`SKILL.md`](translate-native/SKILL.md) according to that platform's skill instructions.

Invoke it explicitly:

```text
$translate-native
```

Direct skill address:

```text
https://github.com/Maykbiletti/translate-native/tree/main/translate-native
```

## Protect machine-readable content

The zero-dependency guard compares source and target files:

```bash
python3 translate-native/scripts/translation_guard.py source.md target.md
python3 translate-native/scripts/translation_guard.py source.json target.json --format json
python3 translate-native/scripts/translation_guard.py source.html target.html --format html
python3 translate-native/scripts/check_diacritics.py --language sv target.md
```

It blocks delivery when it detects:

- missing, added, or renamed placeholders;
- changed URLs, email addresses, code, HTML tags, escapes, or format tokens;
- changed JSON keys, hierarchy, arrays, scalar types, or non-string values;
- changed ICU argument names, formatters, selectors, or number signs;
- changed HTML structure, technical attributes, comments, scripts, or styles while still allowing native translation of linguistic accessibility attributes;
- target text that is not valid UTF-8 and Unicode NFC.

The guard protects structure. The agent's seven-pass review protects meaning and native quality.

The diacritics linter additionally catches frequent ASCII substitutions such as `schoen`, `forstar`, `informacion`, and `cestina` while leaving code, URLs, and other protected technical spans alone. It is a deterministic warning system, not a finite definition of any language; the built-in orthography gate remains mandatory for every script worldwide.

Language tags without a deterministic substitution list, such as `en`, are accepted. The linter still checks UTF-8 and Unicode NFC, prints that no language-specific diacritics rules apply, and leaves the mandatory native-orthography review in force.

For complete HTML pages, linguistic metadata may change: descriptions, keywords, application names, Open Graph titles/descriptions, and Twitter titles/descriptions are treated as translatable text while their placeholders remain protected. Technical metadata such as viewport settings, encodings, URLs, and unrelated `content` attributes remain fixed.

## Evidence, not dataset worship

| Source | Best use | Warning |
| --- | --- | --- |
| [Language communities and authorities](translate-native/references/native-translation-standard.md) | Orthography, terminology, accepted standard | Prefer the requested community's own convention |
| [Unicode CLDR](https://cldr.unicode.org/) | Locale IDs, formats, plurals, exemplar characters | Locale data is not prose guidance |
| [Unicode Normalization](https://unicode.org/reports/tr15/) | Canonical Unicode normalization | NFC does not prove correct spelling |
| [W3C Language Enablement](https://www.w3.org/International/typography/gap-analysis/language-matrix.html) | Script layout and typography | Web support data is not a dictionary |
| [IANA Language Subtag Registry](https://www.iana.org/assignments/language-subtag-registry) | Language, script, and region tags | Tags identify a target; they do not translate it |
| [Leipzig Corpora Collection](https://cls.corpora.uni-leipzig.de/) | Native usage and collocations | Check domain and date |
| [FLORES+](https://huggingface.co/datasets/openlanguagedata/flores_plus) | Multilingual evaluation | It is an evaluation set, not training data |
| [OPUS](https://opus.nlpl.eu/) | Supporting parallel examples | Large corpora can contain literal or noisy translations |

Hugging Face is a useful distribution platform, not a quality certificate. Provenance, curation, locale, license, domain, and intended use still matter.

## Repository structure

```text
translate-native/
├── SKILL.md
├── agents/
│   └── openai.yaml
├── assets/
│   └── icon.svg
├── references/
│   ├── native-translation-standard.md
│   ├── native-orthography.md
│   ├── evaluation-protocol.md
│   ├── translationese-review.md
│   └── structured-content.md
└── scripts/
    ├── translation_guard.py
    ├── check_diacritics.py
    └── blun_language_guard.py
```

The MCP configuration example, persistent CLI rules, automated tests, and GitHub Actions workflow live at repository level.

Machine-readable failure cases live in [`evals/regressions.jsonl`](evals/regressions.jsonl). They cover Swedish translationese and model selection, German umlauts, Spanish punctuation and accents, Czech and Catalan orthography, Vietnamese tone marks, Chinese and Arabic native scripts, and Ukrainian language identity. They are regression examples, never a language allowlist.

## Test

```bash
python3 -m unittest discover -s tests -v
```

The suite covers protected text, sentence punctuation after URLs, JSON structure and types, ICU plural/select syntax, safely translatable HTML attributes, HTML/code tampering, Unicode normalization, common missing diacritics in German, Spanish, and Czech, technical-span protection, the combined language-and-orthography contract, repository agent instructions, and both Swedish agent-copy regression cases.

## Design principle

Translation is not token replacement. It is constrained re-authorship: the same meaning, rebuilt inside another language's own system.

That standard is universal. Confidence is not.

## License

Released under the [MIT License](LICENSE).

---

<div align="center">

### Built for every language that people call home.

**BLUN**

*Get it done with BLUN.*

</div>
