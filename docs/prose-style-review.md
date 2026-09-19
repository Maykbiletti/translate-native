# Style and native quality for original writing and translations

The deterministic language report now includes a separate `style_review`.
Its top-level `PASS` still describes the existing orthography/Unicode checks;
it does not certify style, semantic quality or human authorship.

The versioned `prose-style-v1` diagnostic measures repeated lexical sentences
and unusually uniform sentence lengths in supported word-delimited languages,
including all EU official languages. German additionally has an explicit stock
transition profile. No German phrase list is applied to other languages.
Findings contain exact character offsets, total occurrence counts and at most
ten example spans per finding. The report binds the complete original text by
SHA-256, locale, content type and measurement-profile version.

`REVIEW_RECOMMENDED` means surface signals need contextual judgment.
`NO_SIGNALS` is not a style approval. `NOT_ASSESSED` explicitly identifies short
inputs, unsupported formats or unsupported segmentation (for example Chinese).
Code, Markdown blockquotes, headings, lists and tables are excluded from these
surface measurements. Sentence segmentation is heuristic, not linguistic proof.
The measured thresholds are conservative engineering defaults, not calibrated
human-authorship or quality classifiers. Inline quotations and intentional
rhetorical repetition still need editorial judgment.

All languages and original target-language writing use the existing isolated,
source-blind native reviewer. Its task now explicitly includes whole-text
information progression, paraphrased repeated theses, redundant conclusions,
empty transitions and monotonous patterns. It must assess these against the
locale, audience, register and genre, cite concrete passages and explain reader
impact. For translations the independent source-aware fidelity stage follows.
Surface measurements cannot detect semantic paraphrases or replace that review.

The writer revises the candidate using concrete review findings. Every changed
candidate requires fresh exact-text review under the existing host-bound process.
Operators adopting these instructions must advance their prompt/software policy
versions and invalidate prior approvals and caches under the existing versioned
policy contract. The response adapter also binds the exact task instructions;
website worker request IDs now include the exact system-instruction hash.
The existing Guard remains the release authority. Reviewers never infer who
wrote the text, invent native evidence or add errors to make writing look human.
Unavailable native review remains blocked by the existing release path. Advisory
surface flags alone do not reject deliberate refrains, plain language or legal
terminology. Existing budgets and correction limits remain in force.

Tests use synthetic fixtures, including long prose and Finnish/Maltese adapter
calls. They prove measurement and protocol behavior only. The reported original
29,705-character text has not been supplied and has not been tested here.
