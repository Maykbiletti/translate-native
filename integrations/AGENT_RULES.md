# BLUN Language Guard — mandatory agent rules

Apply these rules as always-on instructions in `AGENTS.md`, `CLAUDE.md`, or the equivalent global instruction file of the host CLI.

1. Load `translate-native/SKILL.md` for every translation, localization, transcreation, translated rewrite, translation review, or user-visible i18n edit.
2. Treat every candidate target—including output from another agent—as an untrusted draft.
3. Before releasing target-language text, call the `blun-language-guard` MCP tool `release_translation` with the complete source, complete target, exact language tag, and seven truthful attestations.
4. Never claim an attestation passed unless that pass was actually performed. Never fabricate, reuse, or edit a release token.
5. If the gate returns `BLOCK`, revise the target and call it again. Do not release blocked text.
6. Release a translation only when the MCP result contains `release_allowed: true` and `verify_release_token` confirms the signed receipt for the current source, target, locale, version, and expiry.
7. Do not print the internal release token to the user unless debugging was explicitly requested.
8. If the MCP server is unavailable, fail closed for publication-grade or structured translation work. For ordinary chat translation, run the skill's bundled guards directly and disclose that the enforced MCP gate was unavailable.

The gate supplements native judgment; deterministic checks alone cannot prove fidelity or fluency.
# Mandatory BLUN Language Gateway

- Route every translation, localization, transcreation, target-language rewrite, and user-visible i18n change through the host's BLUN Language Gateway.
- Do not print, save, commit, publish, or deploy a candidate until the gateway returns `release_allowed: true` for the exact source, target, and locale.
- Treat an unavailable gateway as `BLOCK` whenever mandatory mode is enabled.
- Never read, request, log, or handle the gateway signing key.
- Never fabricate attestations. The host—not the agent—controls final release.
