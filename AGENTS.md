# Agent instructions

## Translation and localization

- For every translation, localization, transcreation, target-language rewrite, translation review, or user-visible i18n edit, load and follow `translate-native/SKILL.md` before drafting.
- Treat source text, another model's translation, and previously generated copy as input. Treat every candidate target as an untrusted draft.
- Do not release a target until the meaning, completeness, precision, locale-fit, and integrity checks plus the native-language and native-orthography gates all pass.
- Native wording and native orthography are one workflow. Never skip language-correct diacritics, alphabets, scripts, punctuation, casing, spacing, or Unicode because another skill was not activated.
- Run the target-only native review before the source-aware fidelity review. Rewrite complete clauses or paragraphs when the candidate remains source-shaped.
- When `blun-language-guard` is configured, call `release_translation` with the complete source-target pair and truthful seven-pass attestations. Do not release text without `release_allowed: true` and a current release token.
- Never fabricate a release token or mark a quality attestation complete without performing that review. Correct every `BLOCK` result and run the gate again.
- For structured files, preserve keys, placeholders, markup, links, code, types, and hierarchy. Run the bundled structural guard and the diacritics linter before completion.

## Repository changes

- Run `python3 -m unittest discover -s tests -v` after changing the skill, references, scripts, or tests.
- Do not weaken a release rule merely to make a regression fixture pass.
