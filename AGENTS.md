# Agent instructions

## Every user-visible response

- Treat every natural-language answer as an untrusted candidate, including ordinary chat replies that are not translations.
- When `blun-language-guard` is configured, call `release_response` with the complete final answer and exact host-supplied language tag. The trusted host must inject a one-time review context and delegate a source-blind native-language review; the writing agent cannot attest or approve its own answer.
- Deliver only when the exact current text receives `release_allowed: true` and a purpose-bound release token. Any edit after validation invalidates the release.
- Never use `auto`, `all`, another language tag, or the response path to hide missing native characters. The host owns `task_kind` and the expected language; the agent must not choose them to obtain a pass.
- A strong installation must intercept output outside the agent and fail closed. MCP instructions alone are behavioral guidance, not a non-bypassable security boundary.
- Never invent, copy, or reuse a response-review context. Missing host review, low confidence, reviewer self-approval, changed text, wrong locale, replay, timeout, or adapter failure blocks release.
- When `BLUN_LANGUAGE_GUARD_MANDATORY=1`, return exactly one JSON envelope containing only `target_text` and the purpose-bound `release_token`; never call a delivery channel directly or place host-owned task, locale, source, or policy fields in the envelope.
- Treat streaming deltas, logs, progress text, and alternate senders as delivery channels. In mandatory mode, candidate prose belongs only inside the final signed envelope.
- Never read the isolated guard token or signing key. If the required service is unavailable, stop instead of falling back to local verification.

## Translation and localization

- For every translation, localization, transcreation, target-language rewrite, translation review, or user-visible i18n edit, load and follow `translate-native/SKILL.md` before drafting.
- Treat source text, another model's translation, and previously generated copy as input. Treat every candidate target as an untrusted draft.
- Do not release a target until the meaning, completeness, precision, locale-fit, and integrity checks plus the native-language and native-orthography gates all pass.
- Native wording and native orthography are one workflow. Never skip language-correct diacritics, alphabets, scripts, punctuation, casing, spacing, or Unicode because another skill was not activated.
- Run the target-only native review before the source-aware fidelity review. Rewrite complete clauses or paragraphs when the candidate remains source-shaped.
- When `blun-language-guard` is configured, call `release_translation` with the complete source-target pair and truthful seven-pass attestations. Do not release text without `release_allowed: true` and a current release token.
- Never send a translation through `release_response`. Load and apply `translate-native/SKILL.md`, then use `release_translation`; the trusted host must provide `task_kind: translation` and the complete source.
- Never fabricate a release token or mark a quality attestation complete without performing that review. Correct every `BLOCK` result and run the gate again.
- For structured files, preserve keys, placeholders, markup, links, code, types, and hierarchy. Run the bundled structural guard and the diacritics linter before completion.

## Same-language natural rewriting

- Route an original or AI draft that stays in the same language through `task_kind: rewrite` and `rewrite_text`, never through response or translation merely because it has source text.
- The trusted host must bind the complete original, exact locale, registered profile, stable request ID, session epoch and writer identity in a one-time rewrite context before any creator or reviewer starts. The model must not select its own profile or dialect.
- Use a dialect profile only when the user requested that variety. Missing, forged, replayed, stale or conflicting context blocks before model work.
- Deliver only the exact Guard-returned target with its rewrite-purpose receipt. The target-only native review runs before the separate original-preservation review; any later edit requires both reviews again.
- Never shorten or summarize a long original to fit one model call. Only the trusted worker may segment plain text; release requires exact reassembly evidence plus fresh whole-document native and preservation reviews. Long structured input blocks until a structure-aware segmenter exists.

## Repository changes

- Run `python3 -m unittest discover -s tests -v` after changing the skill, references, scripts, or tests.
- Do not weaken a release rule merely to make a regression fixture pass.
