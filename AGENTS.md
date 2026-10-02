# Agent instructions

## Ordinary conversations

- Use native spelling, diacritics, alphabets and punctuation in normal answers.
- Ordinary `response`/`chat` output does not require `release_response`, a second model call, a receipt or a delivery envelope. Legacy `BLUN_LANGUAGE_GUARD_MANDATORY=1` must not enable this requirement.
- Response review is optional only when the trusted host explicitly requests the new `BLUN_LANGUAGE_GUARD_RESPONSE_REVIEW=1` setting (King adapter: `languageGuardResponseReview: true`). Do not turn it on automatically.
- Published translations and requested same-language revisions still require their dedicated release paths. Never relabel those tasks as ordinary chat to evade review.

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
- Never shorten or summarize a long original to fit one model call. Only the trusted worker may segment plain text or a specifically supported structured profile; release requires exact reassembly evidence plus fresh whole-document native and preservation reviews. JSON, the strict YAML localization-mapping subset, the documented HTML subset, the versioned Android-resource and simple XLIFF-target XML selectors, conservative long Markdown, strict GNU-PO `msgstr`, strict Apple `.strings` values, and strict SRT/WebVTT cue payloads have structure-aware paths. Every other long structured input remains blocked.

## Repository changes

- Run `python3 -m unittest discover -s tests -v` after changing the skill, references, scripts, or tests.
- Do not weaken a release rule merely to make a regression fixture pass.
