# BLUN Language Guard — mandatory agent rules

Apply these rules as always-on instructions in `AGENTS.md`, `CLAUDE.md`, or the equivalent global instruction file of the host CLI.

1. Treat every user-visible natural-language answer—including ordinary chat—as an untrusted candidate.
2. For an agent's own answer, call `release_response` with the complete final text, the exact host-supplied language tag, and truthful nativeness and orthography attestations.
3. Load `translate-native/SKILL.md` for every translation, localization, transcreation, translated rewrite, translation review, or user-visible i18n edit.
4. For a translation, never call `release_response`. Call `release_translation` with the complete source, complete target, exact language tag, and seven truthful attestations.
5. Never claim an attestation passed unless that pass was actually performed. Never fabricate, reuse, edit, or switch the purpose of a release token.
6. If either gate returns `BLOCK`, revise the candidate and call the correct gate again. Do not release blocked text.
7. Release text only when `verify_release_token` confirms the signed receipt for the exact current text, task kind, source when applicable, locale, version, and expiry.
8. Do not print the internal release token to the user unless debugging was explicitly requested.
9. Never choose `task_kind`, omit a translation source, or substitute `auto`, `all`, or another language to obtain a pass. Those values belong to the trusted host adapter.
10. If mandatory mode is enabled and the gateway is unavailable, fail closed. A direct MCP call without a host output interceptor is advisory because an agent can skip it.

The server injects the same policy through MCP initialization instructions and exposes a `translate-native` prompt. Hosts that support an installed skill/plugin must activate it for translations. Hosts that do not support skills still receive the MCP workflow, but deterministic checks alone cannot prove fidelity or fluency.
# Mandatory BLUN Language Gateway

- Route every user-visible natural-language answer through the host's BLUN Language Gateway.
- Set `task_kind: response` for an agent's own answer and `task_kind: translation` for every translation, localization, transcreation, target-language rewrite, or user-visible i18n change.
- For translations, supply the complete source and activate the installed `translate-native` skill/plugin before drafting. Never downgrade a translation to `response`.
- Do not print, save, commit, publish, or deploy a candidate until the gateway returns `release_allowed: true` for the exact target, task kind, locale, and source when applicable.
- Treat an unavailable gateway as `BLOCK` whenever mandatory mode is enabled.
- Never read, request, log, or handle the gateway signing key.
- Never fabricate attestations. The host—not the agent—controls final release.
