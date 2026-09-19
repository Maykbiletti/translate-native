# Natural revision of original writing

The `rewrite_text` MCP tool and authenticated Guard service operation execute a
real provider-neutral pipeline, not just style diagnostics:

1. An operator-configured creator revises the complete original.
2. The existing host-subagent adapter delegates target-only native review. No
   original, source hash, job ID, glossary notes or creator history enters that
   model context; only the target and independently authored target profile do.
3. A distinct isolated reviewer compares the original and revision for meaning,
   completeness, numbers, negation, modality, protected syntax and personal voice.
4. Deterministic structure/Unicode checks run and the Guard alone signs the exact
   original, output, locale, content type, profile and evidence digest.

The worker never receives the signing key or publication privileges. Ordinary
responses without an original retain their existing native-review path.
Translations retain their existing source-identity and volume requirements.
Same-language rewriting allows a legitimate unchanged passage or concise revision;
it does not disable any translation rule.

## Operator configuration

Run the isolated service with `--rewrite-worker-factory package.module:factory`.
The trusted factory returns a mapping of explicit profile IDs to
`integrations.native_rewrite_worker.NativeRewriteWorker` instances. This follows
the existing host-owned reviewer-factory deployment pattern; never load a factory
from request data or a model-writable directory. Configure the creator and real
host-subagent adapter outside the writing agent's sandbox. No provider is built in.

Each worker takes an owner-only durable `ledger_path`, creator identity/session,
model identity/version, host-policy version, and a source-free profile:

```json
{
  "locale": "fi-FI",
  "audience": "General readers",
  "tone_profile": "Clear contemporary Finnish; preserve the author's voice",
  "target_terms": [],
  "profile_version": "project-fi-1",
  "prompt_version": "rewrite-1",
  "software_version": "deployment-1"
}
```

The optional `dialect` field is an explicit operator profile, selected only on
user request. Arbitrary exact BCP-47 locales are supported by the contract, not a
finite EU allowlist. This does **not** establish linguistic competence in every
locale. The profile example intentionally lacks production evidence and therefore
blocks creation until the operator adds a verified `native_evidence` record:
`version`, `sha256`, `locale`, `dialect` (empty for standard language),
`content_types`, `reviewer_kind` and `reviewer_id`. The kind must be
`qualified_native_reference` or `independent_model_evaluation`; the latter must
identify a different model. The trusted factory must resolve and authenticate
the actual reference/evaluation record, not fabricate a digest or accept a model's
claim. These records are host configuration, never caller input. Missing or
inapplicable records block for independent review before a model starts.
Operators must supply reliable locale/variety evidence and a host that
enforces isolation, deadlines, token budgets and durable execution deduplication.
Low confidence or conflicting findings block for independent model or qualified
native-speaker review; two instances of the same model do not provide independent
model evidence.

For long plain-text work, the provider-neutral creator adapter has two additional
trusted methods outside model output. `verified_completion(request, response)`
returns exactly `schema`, the canonical request and response SHA-256 values,
normalized `finish_reason`, measured `output_tokens`, and an opaque
`provider_execution_id`. `verify_completion(evidence, request, response)` must
locally verify the provider receipt or other authenticated execution record and
return the exact boolean `true`. A model-authored field, guessed token count or
unsigned copy is not sufficient. Both methods are mandatory only for the long
segment contract; legacy short-rewrite adapters remain compatible. Deployments
with output ceilings below 2,048 tokens keep the short path but block long input
before model access with `rewrite.long_document_budget_insufficient`.

Rewrite reviews use the strict
`translate-native.native-rewrite-review.v3` result schema. Every defect is in a
severity-specific list and repeats its `severity`, `class`, exact `excerpt`,
`reason`, reader/meaning `impact`, and actionable `revision_direction`.
An excerpt must occur in the reviewed candidate, or—for an omission found by
the fidelity reviewer—in the bound original. Unanchored passages are invalid.
Uncertainty is not hidden in a score: each entry requires `class`, `reason`, and
`evidence_needed`. `confidence: low` without an uncertainty is invalid; any
uncertainty makes `PASS` invalid and returns
`rewrite.uncertainty_requires_review`. Missing or inapplicable configured native
evidence returns `rewrite.native_evidence_required`. Malformed, incomplete, or
severity-inconsistent reports return `rewrite.review_invalid`. These states never
trigger a correction or release. Review reports remain internal evidence, not
user-facing text.

The source-blind target-language phase also returns a required holistic
assessment for the complete candidate: whether it reads as original native
writing, why, and whether repair is local, passage-wide, or whole-text. A
negative holistic assessment caused by a failed dimension must be anchored in
at least one concrete major or blocking defect and cannot be reduced to a
spelling correction. A target-native
`PASS` additionally requires a positive whole-text assessment with no repair.
The assessment separately covers idiom and word choice, syntax and information
flow, rhythm and cohesion, register/tone/audience, and voice/genre/intentional
repetition. Every dimension is `PASS`, `FAIL`, or `NOT_ASSESSED`; the aggregate
can pass only when all five pass. A failed dimension requires an anchored defect.
`NOT_ASSESSED` requires an explicit uncertainty and evidence request, does not
trigger automatic correction, and remains fail-closed. The dimensions are
interpreted through the requested locale and profile; they do not impose a
German or English style norm on other languages.

## API and delivery

The authenticated service accepts:

```json
{
  "operation": "rewrite_text",
  "request_id": "document-42-revision-3",
  "profile_id": "project-fi-standard",
  "language": "fi-FI",
  "content_type": "prose",
  "source_text": "The complete original Finnish text goes here.",
  "rewrite_context_token": "host-issued one-time token",
  "session_id": "host-session",
  "session_epoch": "64 lowercase hexadecimal characters",
  "agent_id": "writer identity"
}
```

The placeholder above describes the field; it is not a Finnish quality fixture.
Before this call, the trusted host invokes `prepare_rewrite_context` outside the
model with the exact original, locale, profile, request/content type, current
registered session epoch and writer identity. The resulting token is valid once
and for 180 seconds. Missing, forged, replayed, stale or modified bindings block
before the creator or reviewers receive text. MCP `rewrite_text` accepts the same
fields except `operation`; its context token must be host-injected. The existing
language gateway accepts `task_kind: rewrite` with those fields. No candidate,
attestation or reviewer identity supplied by the caller can grant release.

Success returns `target_text`, `release_token` with purpose `rewrite`, profile
and evidence hashes, and an advisory style report. Failed reviews return no draft.
Portable `verify_release_token` confirms the receipt's cryptographic and exact-text
binding only; it does not authorize delivery. Rewrite delivery still requires the
current host-bound session identity and a fresh one-time authorization grant.
Use `integrations/adapters/native_rewrite.py` from a trusted host: construct
`NativeRewriteClient(call_service)` with the existing authenticated service
transport. Register the current session epoch once, then pass that session,
epoch and writer identity to `rewrite(...)`; the adapter prepares the one-time
context before `rewrite_text`. `deliver(...)` verifies the receipt
against the current profile, authorizes and consumes a fresh session-bound
one-time grant immediately before passing unchanged text to the send callback.
Never trim, normalize, or edit the approved text. A transport error after send
requires reconciliation, not blind retry. Internal tool results are not a license
to bypass the user's output interception boundary.

The Claude plugin requires the trusted host to set
`BLUN_LANGUAGE_GUARD_TASK_KIND=rewrite`, an exact
`BLUN_LANGUAGE_GUARD_LANGUAGE`, and the registered
`BLUN_LANGUAGE_GUARD_PROFILE_ID`. It must also set the complete original as
canonical UTF-8 base64 in `BLUN_LANGUAGE_GUARD_REWRITE_SOURCE_B64`, a stable
`BLUN_LANGUAGE_GUARD_REWRITE_REQUEST_ID`, and one supported
`BLUN_LANGUAGE_GUARD_REWRITE_CONTENT_TYPE`. PreToolUse replaces model-supplied
source, request, content type, task, locale and profile with those host-owned
bindings. PostToolUse takes the final candidate only from the authenticated
Guard result—not the tool input—then obtains a one-time rewrite delivery grant.
Stop/SubagentStop accepts only the byte-identical target for the same session,
agent, request, original hash, locale, profile, content type and Guard boot. Each
rewrite receipt may authorize only one delivery grant. Missing policy, a failed
tool call, a changed result, or an unavailable Guard blocks delivery. Existing
response and translation paths remain compatible. No live installation or host
configuration is changed by this implementation.

If the authorization response is lost, an exact retry with the same bound
request returns the same grant rather than minting another one. A changed retry
blocks. Grant consumption remains one-time; an ambiguous transport result after
consumption requires reconciliation instead of automatic resend.

## Bounds and evidence

The worker permits **at most one editorial correction**, configurable by the
operator as `max_corrections=0` or `1` (default `1`). Only a host-verified,
high-confidence native review with major defects, no blocking defects, and exact
excerpts found in the candidate can trigger it. Low confidence, legal content,
unanchored findings, conflicting or malformed reports, receipt errors and
original-preservation failures still require independent review; they do not
trigger another model attempt. This is not a second independent model adapter.

The creator receives the original, the rejected candidate and the structured
editorial report as untrusted data, without host attestation/control metadata.
The corrected candidate must change, preserve protected structure and pass a new
source-blind native review followed by original-versus-result review. Neither
the original nor the earlier report enters the new native review. A second
failure blocks; no third draft is generated. Only the final accepted text can
receive a Guard receipt. The signed evidence digest also commits to the rejected
candidate hash and its host-verified review, retained internally, not published.

For a short original, an uncorrected run uses at most three model calls. A
corrected run uses at most five: two creations, two native reviews and one
preservation review. Each call keeps the configured timeout/output-token ceiling;
creator and host adapters must enforce those budgets. At the default 60-second
ceiling, short model work is bounded by 300 seconds. MCP transport allows 1,510
seconds for the maximum configured long-document budget plus overhead.

Plain long originals use a separate policy-bound pipeline. Before model access,
the trusted worker creates at most ten ordered, source-owned segments and keeps
their exact whitespace separators outside model control. Each segment response
must carry the exact segment ID and `completion_status: complete`; truncation,
wrong order, a missing segment, changed separator, stale response or ambiguous
in-flight result blocks. A separate trusted creator-adapter record must bind the
exact request and response hashes, normalized `finish_reason: complete`, output
token count and provider execution ID. Model JSON cannot attest its own finish
reason; missing, length-limited or forged provider completion metadata blocks.
Segment responses and completion records are reserved and persisted before the
next call, so restart reuses only an exact completed response and never launches
it twice. The worker assembles the whole revision once, then sends that complete
target to the source-blind native reviewer. That reviewer receives no source
text, source hash, source-derived manifest, segment count or creator context—only
the complete target plus the allowed locale profile. Its host receipt and worker
evidence bind the exact reviewed target hash without exposing source metadata.
A distinct fidelity reviewer then receives the complete original and assembled
revision. Per-segment model judgments never authorize release.

The deterministic manifest binds every source range, source hash, separator,
target range, target hash, creation request/response hash and trusted completion
record. It remains internal to the worker and Guard. The Guard reconstructs every
segment request and response, then recomputes the plan and exact assembly and
requires both final reviews to bind the same assembled-target hash before it can
sign. One document-wide correction may recreate only segments unambiguously
owning a concrete native-review excerpt; all reused segment evidence is retained,
and the complete newly assembled result receives both reviews again. Findings
that cannot be assigned to exactly one segment require independent review rather
than a risky automatic edit.

The output-token ceiling determines the segment size; the configured per-call
timeout determines how many segments fit the fixed 1,500-second aggregate bound.
At defaults this permits ten segments of up to 3,072 Unicode characters, for at
most 23 calls in the worst plain-text correction path. A document beyond the
computed bound returns `rewrite.long_document_too_large` before model access.
Unsupported structured containers return
`rewrite.long_document_structured_unsupported`; they are never silently
summarized or routed through the short path. Long XML has the separate bounded
Android-resource path described below, Markdown has a separate conservative
raw-span path, GNU PO has a strict non-empty-`msgstr` path, Apple `.strings` has
a strict non-empty-value path, and SRT/WebVTT has a strict cue-payload path.
Token/cost reservation must use the
computed long plan, not assume five calls. Segment policy, budget, correction
limit and all instructions are part of the effective profile hash, so a policy
change invalidates previous receipts.

Long JSON uses a separate raw-token plan. A bounded strict parser rejects
duplicate decoded keys (including Unicode-escape aliases), non-finite numbers,
lone surrogates, excessive depth, excessive string-value count and trailing data
before model access. Extreme finite exponents are compared without host-float
overflow or underflow. Typed, escaped JSON-pointer identities cannot collide when
keys contain dots, brackets, slashes or tildes. Only decoded string-value parts,
opaque value IDs and bounded read-only context from the same string value enter
creator requests. Decoded keys and paths, delimiters, indentation, member order,
array order, number lexemes, booleans and null remain host-owned. Valid top-level
JSON scalars use this plan; bracketed prose such as `[Refrain]` or `{name}` is
not guessed to be JSON, while unambiguously JSON-shaped strict-parser failures
remain fail-closed.
The creator must return the exact ordered value-ID set for each bounded batch;
missing, additional, duplicated or reordered values and incomplete provider
completion evidence block. The trusted worker JSON-escapes each changed value;
an unchanged value retains its original escape token and casing byte-for-byte.
It replaces only its original value token and proves the immutable container
skeleton again in the Guard. The source-blind reviewer then receives only the
complete assembled target JSON and the allowed target profile; fidelity receives
the complete original and target afterward. Combined source-plus-target fidelity
review text is capped at 262,144 UTF-8 bytes: capacity is reserved before creator
access and the exact result is checked again after assembly.
Automatic correction is deliberately
disabled for long JSON until review findings carry a host-verifiable unique value
identity; an actionable finding therefore routes to independent review rather
than risking the wrong repeated value.

Long GNU PO uses a strict raw-token plan. It accepts UTF-8 NFC catalogs whose
active directives and continuation strings use a bounded JSON-compatible subset
of GNU PO quoting. Only non-empty `msgstr` and `msgstr[n]` values outside the
empty-`msgid` metadata header enter creator batches. Comments, flags, obsolete
commented entries, contexts, singular and plural msgids, plural indexes,
keywords, whitespace and line endings remain host-owned. Empty translations are
not invented. Unsupported escapes, orphaned continuations, duplicate fields,
unknown active syntax, noncanonical plural indexes and catalogs without a
rewritable value block before creator access.

Every decoded value part has an opaque ordered ID and bounded context from that
same value. Missing, additional, duplicated or reordered results block. An
unchanged value retains every original quoted token byte-for-byte; a changed
value is safely re-escaped while the original number and placement of string
tokens, all directive text and the immutable catalog skeleton are revalidated.
Protected-token signatures are compared per decoded `msgstr` value, so an
unchanged placeholder in a `msgid` cannot conceal a dropped placeholder in a
continued translation string.
The native reviewer receives a deterministic ordered projection containing only
the complete decoded non-empty `msgstr`/`msgstr[n]` prose from the assembled
candidate. It never receives `msgid`, `msgid_plural`, comments, contexts, catalog
headers, paths, source hashes or creator context. The separate preservation
review receives the exact original and complete assembled catalog. Review
evidence binds both the projection hash and the exact full-target hash; the Guard
recomputes the projection, PO plan, creator requests, completion evidence and
exact assembly. Combined source-plus-target review input is capped at 262,144
UTF-8 bytes, and automatic document correction remains disabled until a finding
can be bound safely to one exact PO value.

Long Apple `.strings` uses a strict raw-token plan. It accepts UTF-8 NFC files
containing unique quoted keys, quoted values, semicolon terminators and bounded
line or block comments. Only decoded non-empty values enter creator batches.
Keys, comments, empty values, separators, quote spelling, whitespace and line
endings remain host-owned exact bytes; changed values are safely re-escaped.
The parser supports the documented simple escapes plus four-digit `\\u` and
`\\U` escapes, validates surrogate pairs, and rejects unknown escapes, raw
control characters, duplicate decoded keys, comments inside assignments,
missing delimiters and unterminated constructs before creator access.

Each Apple value part has an opaque ordered ID and bounded context only from
that same value. The worker rejects missing, extra, reordered or incomplete
results, rebuilds the immutable skeleton and separately checks placeholders and
format specifiers inside every decoded value. The isolated native reviewer
receives a deterministic ordered projection containing only decoded non-empty
localized values. Keys, comments, empty entries, separators, layout, source
hashes and creator context are absent. The preservation reviewer then receives
the exact original and complete target. Review evidence binds both the projection
hash and the exact full-target hash; the Guard recomputes both together with the
selector, manifest, requests, completion evidence and assembly. The same
262,144-byte combined review ceiling applies. Automatic correction remains
disabled until a finding can be bound safely to one exact value.

Long SRT and WebVTT use a strict lossless cue-payload plan. Only non-empty cue
text enters creator batches under opaque ordered IDs with bounded adjacent-cue
context. File headers, cue identifiers, timestamps and settings, WebVTT
`NOTE`/`STYLE`/`REGION` blocks, blank lines and every source line-ending byte
remain host-owned. Inline tags, entities, URLs, email addresses, placeholders,
printf tokens, escapes and inline code are replaced with collision-resistant
host markers before creator access and restored byte-for-byte afterward. Each
cue must keep its original line count.

The parser accepts only bounded UTF-8 NFC SRT or WebVTT with exact timestamp
syntax, unambiguous blank lines and no unsupported cue-edge whitespace. Missing, additional, duplicated or reordered
values, marker changes, line-count changes, timestamp edits, unsupported syntax
or incomplete completion evidence block fail-closed. ASS/SSA `Dialogue:` input
is explicitly unsupported in this rewrite profile and never falls back to plain
text. The complete assembled subtitle receives the source-blind native review in
playback order and then the separate original-preservation review. The Guard
rebuilds the plan, request and response hashes, exact assembly and immutable
container skeleton before signing. Deterministic language checks inspect only
cue prose: ASCII-heavy timing, identifiers, settings and protected technical
syntax cannot create a false script mismatch, while their separate structural
checks remain mandatory. The combined source/target review ceiling is 262,144
UTF-8 bytes, and automatic correction remains disabled until a finding can be
bound safely to one exact cue.

Long HTML uses a separate bounded raw-span plan and never reparses then
serializes a DOM. Standard HTML vocabulary has explicit precedence over XML in
the syntax detector; a well-formed custom-element-only document is classified as
XML and remains blocked until the trusted API carries an explicit container
format. Before any creator call, the parser requires balanced explicit start/end
tags, single- or double-quoted linguistic attributes, ASCII HTML whitespace,
unique attributes, bounded depth
and span counts, and one representation that does not depend on browser error
recovery. It rejects mixed inline content, foreign SVG/MathML namespaces,
templates, textareas, `plaintext`, unsafe declarations, structural template
attributes and executable template delimiters in this first policy version.
Script bodies using legacy escaped/double-escaped or nested-script states also
block instead of relying on an incomplete browser-tokenizer approximation.
Script and style raw text plus code, pre, kbd, samp and
var subtrees stay entirely host-owned. Comments, doctypes, tags, attribute names,
technical attributes, quoting, whitespace, entities, placeholders, URLs and
printf tokens also remain exact source bytes. Only visible text pieces and the
approved linguistic attributes (`alt`, `title`, `placeholder`, ARIA copy and
recognized social/description metadata) enter creator batches under opaque,
ordered span IDs.

The worker accepts only the exact ordered ID set and trusted complete-provider
evidence for every batch. It rejects markup, entities and attribute-quote
injection in candidate values, replaces only the original raw spans, and proves
the immutable skeleton again after assembly. The source-blind reviewer sees only
the complete assembled HTML and allowed target profile; the separate fidelity
reviewer then sees the complete original and target. Combined source-plus-target
review text is capped at 262,144 UTF-8 bytes before creator access and after
assembly. The Guard independently rebuilds the plan, every request/response hash,
completion record and exact assembly before signing. Automatic correction is
disabled because a document-wide finding is not yet bound to one exact HTML
span; actionable findings therefore remain fail-closed for independent review.

Long Markdown uses a versioned conservative raw-span plan. It preserves the
source bytes instead of rendering or serializing a Markdown tree. YAML front
matter, fenced and indented code, complete blockquotes (including lazy
continuations), inline code, link and image syntax, link destinations,
reference definitions, autolinks, URLs, email addresses, entities, escapes,
placeholders, printf tokens, delimiters, list/heading markers, structural and
outer whitespace, hard-break spacing, and line endings remain host-owned. The
first profile intentionally keeps complete
links and bracket labels opaque; it does not rewrite link labels independently.
Only unambiguous prose in headings, ordinary paragraphs and list items enters
creator batches under opaque ordered IDs with bounded context from the same
prose span.

Recognized JSON, PO, Apple strings, subtitles and XML keep their established
format precedence even when their values contain Markdown-like characters.
Validated block HTML that begins at a CommonMark line start keeps precedence
within each balanced region that contains no terminating blank line; ordinary
punctuation in visible HTML and protected `script`, `style`, `pre` and `code`
subtrees therefore causes no heuristic block. Outside those regions, Markdown intent is checked over
a position-faithful projection of all creator-owned HTML spans, so links or MDX
expressions split across inline tags still block before creator access.
Markdown intent detection, flags and that routing order are version-bound in the
effective policy; a change invalidates earlier evidence.

Before creator access, the planner rejects raw HTML, tables, CommonMark and
extension directives, template/MDX statements and expressions, ambiguous front matter,
unclosed front matter/fences/inline code, escaped, multiline, nested or
malformed links, unknown entities, dangling escapes, non-NFC source and
documents beyond the bounded span/group limits. A leading UTF-8 BOM remains
host-owned and is ignored only while recognizing front matter. CommonMark tab
stops determine indented code. Lines containing emphasis-capable delimiters are
kept completely host-owned because delimiter flanking can change rendering.
Reference definitions and their possible continuation/title line, including an
unindented title, remain host-owned. A candidate cannot add Markdown control
characters, a nested block marker at paragraph/list start or line breaks. The
trusted worker reparses the complete assembled target and requires the exact
immutable skeleton. The Guard independently rebuilds the
manifest, every creator request/response and completion record, target mapping
and exact assembly. The same 262,144-byte combined source/target review ceiling
applies before creation and after assembly.

The source-blind reviewer receives only the complete assembled Markdown and the
allowed target profile; the separate preservation reviewer receives the
complete original and target afterward. Automatic document correction is
disabled because a document-wide finding is not yet bound to one unique prose
span. Unsupported or ambiguous Markdown remains fail-closed rather than falling
back to plain-text segmentation.

Long XML is deliberately narrower than generic XML because element names do not
prove that a value is prose rather than a key, checksum, credential or program
fragment. The first version therefore binds one trusted selector profile:
unnamespaced Android `<resources>` containing direct `<string>` values and
`<item>` values directly under unnamespaced `<plurals>` or
`<string-array>`. `translatable="false"` values remain opaque. Any other
non-whitespace text is unclassified and blocks before creator access; a generic
XML document is never guessed to be linguistic.

Direct strings and both collection types require a nonempty `name`. Plural
items require one of Android's `zero`, `one`, `two`, `few`, `many` or `other`
quantities; string-array items cannot carry a plural quantity. XML character
and attribute references are decoded only for these trusted semantic checks,
while their raw spelling remains immutable. Source XML must be NFC before the
creator starts; this makes the final Unicode requirement achievable without
normalizing protected container bytes after the fact.

Android's outer double-quote wrapper is part of the trusted skeleton, including
the whitespace it preserves. The creator sees only its interior and may use an
apostrophe there; it cannot remove the wrapper or introduce an unescaped double
quote. In an unquoted value, an unescaped ASCII apostrophe or quote blocks
before creation and cannot be introduced by a candidate. `translatable=false`
and a namespace-qualified `translate=no` are treated as opaque only on the
recognized string or collection selector elements. They never make an unknown
element such as a script, key or color exempt from linguistic classification.

The raw scanner accepts XML 1.0 UTF-8 source only and preserves the BOM and
declaration, tags, namespace prefixes and bindings, attributes and quote style,
empty-element spelling, whitespace and line endings, comments, processing
instructions, predefined/numeric references, placeholders, URLs, Android
resource/theme references (including `@+id`, private-framework forms and
escaped literals), email addresses and Android backslash escapes as exact
host-owned bytes. It never resolves resources. DTD/DOCTYPE and entity
declarations are rejected before semantic XML validation or generic format
detection. A conforming non-resolving tree parse cross-checks the raw scanner.
XInclude—including a namespace URI written with numeric references—CDATA, unknown named
entities, mixed/inline content, `xml:space="preserve"`, undeclared or duplicate
expanded attributes, namespaced selector lookalikes and malformed XML also
block before creator access.

Only selected raw text pieces enter bounded creator batches under opaque ordered
IDs. The worker requires exact value ordering and trusted provider-completion
evidence, reassembles against the source skeleton, and applies the same
262,144-byte combined source/target review ceiling. The complete XML then
receives the source-blind native review followed by the separate original
preservation review. The Guard independently rebuilds the policy-bound selector,
manifest, requests, completions and exact assembly. Automatic XML correction is
disabled until a finding can be safely assigned to one exact selected value.

Segment cuts are allowed only at explicit whitespace or recognized sentence
terminators. If a long unspaced input has no such safe boundary, the worker
returns `rewrite.long_document_safe_boundary_unavailable` before model access.
It uses the complete, policy-bound Unicode 17.0 `Extend`, `SpacingMark` and `ZWJ`
Grapheme_Cluster_Break table rather than general-category guesses; an Indic
conjunct, Hangul Jamo syllable, Thai spacing mark or emoji tag sequence cannot be
divided between model calls. The corresponding Unicode 17.0 `Prepend` table also
protects leading and trailing document-whitespace boundaries.

The separate durable correction ledger reserves the sole attempt before invoking
the creator, then persists its result before starting fresh reviews. Restarts
reverify the earlier host review and resume a persisted corrected candidate;
ambiguous creation never silently starts another model call. Concurrent callers
cannot allocate another correction. An unchanged request ID with changed input
or profile blocks. Caller edits outside this internal correction require a new
request and fresh reviews. Operator version changes invalidate prior bindings.

Orthography PASS, heuristic style hints and actual review acceptance are separate.
Uniform sentence lengths and transition words are only advisory. `NO_SIGNALS`
does not grant release; unsupported heuristic measurement says `NOT_ASSESSED`.

Tests use labeled synthetic creator/reviewer fixtures through the actual adapter
and Guard API. They prove isolation, ordering, binding and failure behavior, not
native quality or improvement over DeepL. Real native-speaker acceptance and real
provider configuration remain required. The user's 29,705-character original
has not been supplied and is not represented as evaluated. A separate synthetic
29,705-character Finnish-like fixture proves only that all characters cross the
segmented creator, whole-document reviews, Guard signature and receipt verifier
without truncation.

Examples in the synthetic adapter tests include Finnish
“On tärkeää huomata, että teksti on selkeä.” → “Teksti on selkeä.” and Maltese
“Huwa importanti li ngħidu li t-test huwa ċar.” → “It-test huwa ċar.” These are
fixed test inputs/outputs demonstrating removal of an introductory formula, not
independently accepted language-quality examples. Unchanged quotations, refrains,
code and good wording are valid technical counterexamples. Han script checks
recognize `Hans`/`Hant` as Han block membership only; Simplified/Traditional usage
is still the native reviewer's responsibility, not a deterministic quality claim.
This distinction follows [Unicode UAX #24, section 2.2](https://www.unicode.org/reports/tr24/#Relation_To_ISO15924)
and the [ISO 15924 code list](https://www.unicode.org/iso15924/iso15924-codes.html),
checked on 2026-09-18. The existing range heuristic is not a complete Unicode
Script-property implementation; unsupported measurements remain unevaluated.
