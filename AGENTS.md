# Agent instructions

## Every user-visible response

- Treat every natural-language answer as an untrusted candidate, including ordinary chat replies that are not translations.
- When `blun-language-guard` is configured, call `release_response` with the complete final answer, the exact host-supplied language tag, and truthful nativeness and orthography attestations before delivery.
- Deliver only when the exact current text receives `release_allowed: true` and a purpose-bound release token. Any edit after validation invalidates the release.
- Never use `auto`, `all`, another language tag, or the response path to hide missing native characters. The host owns `task_kind` and the expected language; the agent must not choose them to obtain a pass.
- A strong installation must intercept output outside the agent and fail closed. MCP instructions alone are behavioral guidance, not a non-bypassable security boundary.

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

## Repository changes

- Run `python3 -m unittest discover -s tests -v` after changing the skill, references, scripts, or tests.
- Do not weaken a release rule merely to make a regression fixture pass.
