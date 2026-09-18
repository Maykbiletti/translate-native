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

Rewrite reviews use the strict
`translate-native.native-rewrite-review.v1` result schema. Every defect is in a
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

An uncorrected run uses at most three model calls. A corrected run uses at most
five: two creations, two native reviews and one preservation review. Each call
keeps the configured timeout/output-token ceiling; creator and host adapters must
enforce those budgets. At the default 60-second ceiling, model work is bounded
by 300 seconds; the maximum configured ceiling of 300 seconds permits 1,500
seconds. MCP transport allows 1,510 seconds to cover that upper bound plus
overhead. Token/cost reservation must allow up to five calls, not assume three.
The correction limit and instructions are part of the effective profile hash,
so changing the policy invalidates previous receipts.

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
has not been supplied and is not represented as evaluated.

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
