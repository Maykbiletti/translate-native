# Native Translation Evaluation Protocol

## Release rule

Release a translation only when it has zero blocking defects and reads naturally for the specified community, audience, register, and medium. Fluency cannot compensate for changed meaning. Fidelity cannot compensate for obvious translationese.

## Classify defects

| Severity | Definition | Examples | Action |
| --- | --- | --- | --- |
| Blocking | Meaning, safety, structure, or identity is damaged | Reversed negation, missing caveat, wrong number, broken placeholder, invented claim, wrong script | Fix before delivery |
| Major | A native editor would rewrite it or the locale/register is materially wrong | Calque, unnatural syntax, wrong honorific, inconsistent terminology, regional mismatch | Fix before delivery |
| Minor | Meaning and nativeness survive but polish is imperfect | Slight repetition, optional punctuation refinement | Fix when feasible |

Do not average blocking defects into a numeric score. One blocking defect fails the translation.

## Run a target-only native review

Temporarily hide the source and judge the target as original writing. Inspect:

- syntax and information order;
- collocations and idiomatic word choice;
- paragraph flow, cohesion, rhythm, and repetition;
- politeness, honorifics, directness, and social relationship;
- locale, script, punctuation, typography, and medium conventions;
- whether a native writer would choose the same headline, button, instruction, or transition.

Rewrite any passage that is merely understandable but not naturally chosen. Preserve protected tokens while moving them to the grammatically natural position.

## Run a source-aware fidelity review

Compare propositions rather than matching words. Account for every source fact, negation, quantity, entity, relationship, condition, exception, uncertainty marker, time reference, and call to action. Flag:

- omissions, additions, duplication, or unjustified explanation;
- stronger or weaker certainty, obligation, praise, warning, or promise;
- changed causal relationships, scope, chronology, or ambiguity;
- terminology drift and inconsistent names;
- cultural adaptation that changes factual content.

Use back-translation only to reveal possible loss or addition. Never use it as proof of native quality.

## Use independent review when stakes justify it

When another agent or qualified reviewer is available, provide only:

1. the complete source;
2. the candidate target;
3. target language, locale, script, audience, register, and medium;
4. required glossary and protected tokens;
5. this defect rubric.

Ask for a structured defect list with severity, target excerpt, reason, and minimal correction direction. Do not ask the reviewer to replace the whole translation, reveal an expected answer, or praise the draft. Revise centrally so terminology and voice remain coherent.

For legal, medical, safety-critical, contractual, or public high-impact content, an AI reviewer is an additional check, not a substitute for a qualified native professional.

## Route by confidence

- **High:** Language variety and domain are well supported; all gates pass. Deliver normally.
- **Medium:** One or more usage, locale, or domain choices are uncertain. Verify them with authoritative native sources and rerun the review.
- **Low:** Reliable evidence is unavailable or varieties may be mixed. Do not present the result as assuredly native. State the limitation or request native review.

Confidence describes evidence, not how fluent the prose appears.

## Evaluate long documents

Create a translation brief before drafting. Record locale, script, audience, address form, voice, tense policy, glossary, names, units, and formatting. Maintain it across sections. After assembly, review the whole document for:

- terminology and capitalization drift;
- pronoun/reference continuity;
- headings, captions, lists, and cross-references;
- consistent voice, politeness, tense, and narrative distance;
- duplicated or missing content at section boundaries;
- global rhythm and repeated sentence patterns caused by chunking.

Do not declare a long document finished from section-level checks alone.
