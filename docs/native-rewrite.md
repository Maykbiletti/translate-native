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

## API and delivery

The authenticated service accepts:

```json
{
  "operation": "rewrite_text",
  "request_id": "document-42-revision-3",
  "profile_id": "project-fi-standard",
  "language": "fi-FI",
  "content_type": "prose",
  "source_text": "The complete original Finnish text goes here."
}
```

The placeholder above describes the field; it is not a Finnish quality fixture.
MCP `rewrite_text` accepts the same fields except `operation`. The existing
language gateway accepts `task_kind: rewrite` with those fields. No candidate,
attestation or reviewer identity supplied by the caller can grant release.

Success returns `target_text`, `release_token` with purpose `rewrite`, profile
and evidence hashes, and an advisory style report. Failed reviews return no draft.
Use `integrations/adapters/native_rewrite.py` from a trusted host: construct
`NativeRewriteClient(call_service)` with the existing authenticated service
transport. `rewrite(...)` creates/reviews; `deliver(...)` verifies the receipt
against the current profile, authorizes and consumes a fresh session-bound
one-time grant immediately before passing unchanged text to the send callback.
Never trim, normalize, or edit the approved text. A transport error after send
requires reconciliation, not blind retry. Internal tool results are not a license
to bypass the user's output interception boundary.

Existing Claude response/translation hooks remain compatible and unchanged.
They do not recognize rewrite receipts as final-output grants. Use the dedicated
trusted delivery adapter; hosts without rewrite-aware delivery remain blocked.
No live installation or host configuration is changed by this implementation.

## Bounds and evidence

One invocation performs one creation and at most two ordered reviews. There are
no automatic correction loops or repeated model attempts. A changed candidate
must enter a new request and both reviews again. The durable worker ledger and
host execution ledger resume completed work; ambiguous creation never silently
starts a second model call. An unchanged request ID with changed content or
profile blocks. Operator version changes invalidate prior review/release bindings.

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
