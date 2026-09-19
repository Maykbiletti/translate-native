# Commercial localization — profile v4

Use for pricing pages, offer cards, checkout copy, subscription CTAs, discounts,
trials and associated conditions in **any language or script**. The skill is
language-neutral; the website planner currently supplies 24 EU locale profiles.
Technical coverage does not establish native fluency in those languages.

## Keep the offer intact

Before drafting, map each proposition to its product/tier and source segment.
Include headings, neighboring cards, footnotes and linked condition labels.
Preserve:

- amount, currency identity, unit and the product receiving that price;
- discount/saving, comparison basis, eligibility and reference period;
- qualifiers such as “from”, “up to”, “only” and minimum quantities;
- stated tax inclusion/exclusion and scope, without inferring local tax rules;
- billing frequency and amount charged, distinct from an equivalent monthly
  display price or minimum contract term;
- trial cost/duration, introductory period, renewal terms and subsequent price;
- cancellation deadline, notice, fees, refunds and exceptions;
- remaining limits, tier counts, dates, eligibility and attached conditions.

“Four tiers” counts pricing levels, not payment installments. “Save up to” does
not promise every customer that saving. A yearly contract billed monthly is not
a monthly cancellable subscription. Do not add missing terms to improve copy.

## Preserve meaning, allow native presentation

Allow locale-appropriate separators, spacing, currency position, native digits,
number words, written percentages and inflected unit expressions. These must
express the same value and commercial relationship. Do not perform currency
conversion, round prices, or change tax treatment. An explicit request to keep
an exact number/symbol spelling overrides formatting adaptation.

Never interpret `1,234` without reliable locale and source context. “12 months”
and “one year” can be equivalent, but calendar dates, commitment, charging and
renewal conditions must still agree. A digit-count or number-multiset regex is
neither proof of fidelity nor grounds to reject an otherwise equivalent form.

Read protected names and slogans from the project brief/glossary. Protect an
acrostic tagline verbatim when requested; do not translate its initials away.
There is no public default list of brands, tier names, amounts or currencies.

## Review and uncertainty

First hide the source and judge idiomatic commercial language, CTA effect,
register, rhythm, typography and comprehensibility as original native copy.
Then compare the full source and target proposition by proposition. Check both
omissions and added promises. Match each price and condition to its own offer;
equal totals across two cards do not excuse swapped prices.

Record a known mismatch as blocking. If a value, condition, coverage or native
usage remains unresolved, request an independent model adapter or qualified
native/domain reviewer. Do not mark uncertainty as a successful check or invent
a translation to fill a gap. A grammatical target and model confidence alone
are not publication evidence. Neither average scores nor unrelated successful
checks override a blocking or major defect.

For offer-local uncertainty, route the affected review dimension together with
the offer's zero-based registry position. Keep configured offer identifiers out
of the content-free summary. The independent model or qualified reviewer must
resolve that exact ordered scope; never widen, narrow, reorder, or relabel it
after the source-aware review.

## Skill-only evidence check

The executable checker and its shared validator ship in this skill's `scripts/`
directory. No `integrations/` directory, server, model credentials or network is
needed for this local check. Run from the skill directory:

```bash
python3 scripts/check_commercial_review.py --contract
python3 scripts/check_commercial_review.py --source source.txt --target target.txt --review review.json \
  --target-locale sv-SE \
  --commercial-quality-profile-version commercial-eu-sv-SE-2026-09-2 \
  --commercial-quality-profile-sha256 "${COMMERCIAL_QUALITY_PROFILE_SHA256}"
```

The contract response contains the review-input shape plus the content-free,
hashed summary and resolution contracts; it is not a completed review. First perform the
source-blind native review, then have the source-aware reviewer produce the
`commercial_review` object; save that object alone as `review.json`. Do not
manufacture positive evidence to satisfy the checker. Supply the full unchanged
source and target, not extracted price fragments. Inputs must be UTF-8 without
BOM, at most 2,000,000 bytes each. Character spans count Unicode code points, not
UTF-8 bytes or JavaScript UTF-16 units. Line endings are preserved; do not
normalize or edit the files after producing the evidence offsets.

Exit 1 and `BLOCK` mean malformed, changed or unresolved evidence. Missing
arguments exit 2. Exit 0 and `EVIDENCE_VALID` mean only that the evidence meets
the structural contract, **not that prices are correct or the text is ready**.
All machine results say `release_allowed: false`; no token is issued. Successful
checks include hashes of the exact inputs for traceability, not an authenticated
receipt. The independent quality assessment, structural/orthography guards and
host-verified signed release remain necessary. Unknown language/domain facts
must still go to an independent adapter or qualified reviewer.

This helper neither starts nor locates a deployed checking service. The website
worker uses this same validator through a compatibility module in `integrations/`.
The ordinary MCP/response guard is not automatically wired to commercial review
by copying the skill; the trusted host must use the commercial worker path and
enforce its quality and publication gates. Do not claim coverage for a live
BLUN agent without testing that actual path. When updating an installation with
local guard patches, preserve and reconcile those patches; this helper does not
require replacing `scripts/blun_language_guard.py`.

## Website worker contract

The trusted CMS/backend selects `content_type: "commercial"` for complete offer
copy, including its CTA and conditions. Do not let a generating agent downgrade
it to `marketing` or `ui` to avoid review. Keep genuinely legal terms on the
existing `legal` path with required human review; this profile is not a legal
approval. For mixed pages, supply complete contextual commercial units rather
than isolated price fragments.

The planner binds `translate-native.commercial.v13` and the exact target-locale
commercial quality profile into the job and plan IDs.
The authenticated capabilities response publishes the same profile as a
separately hashed, brand-neutral machine-readable contract. It lists all ten
semantic dimensions, exact preservation rules, permitted locale-aware
rendering, the evidence method and the fail-closed route for ambiguity. CMS
integrations can therefore preflight the implemented offer contract without
receiving project prices, product lists, protected terms or deployment secrets.
Its separately hashed `review_summary_contract` also declares the exact six
summary fields, both valid statuses, the only permitted ordered dimension
names, the exact current review-evidence-contract SHA-256, and the evidence-hash
canonicalization. The digest input is a versioned binding containing the
profile, review-evidence-contract digest, target locale, commercial locale-quality
profile version and digest, exact UTF-8 source and target hashes, and the
complete evidence. The summary still explicitly excludes source text, target
text, spans, reviewer prose, project prices and project brands.
Adapters therefore do not need to infer the targeted-review envelope from a
schema name or prose. A changed, missing, reordered or unknown dimension makes
the complete capability response unavailable.

Within every dimension, `offer_statuses` must contain exactly one entry for
every registered offer, in registry order. The dimension-level status is not
free-form: it is derived with `changed`, `uncertain`, `equivalent`, then
`not_present` precedence. Each equivalent, changed, or uncertain offer verdict
must carry evidence located inside that offer's declared regions. This proves
structural completeness of the review, not semantic truth; uncertain values
still require an independent model or qualified native-domain reviewer.

The separately hashed `review_evidence_contract` is the machine-readable
source of truth for the private source-fidelity report. It closes the exact
top-level fields, coverage values, ten dimensions, item relations and limits;
defines Unicode code-point offsets and offer-region containment; and requires
one matched assignment per registered offer. It contains no project prices,
text, spans, brands, or reviewer prose. Its structural validator cannot prove
semantic truth, and the contract explicitly grants no publication authority.
The portable `--contract` response exposes this same object.

The separately hashed `review_resolution_contract` defines the exact ordered
dimension acknowledgement and the two permitted resolution methods. A
qualified-human resolution must omit reviewer provider identity; both methods
must carry the primary provider's exact ID, model ID and model version. An
independent-model resolution additionally carries the same fields for the
second provider, whose provider ID must differ. Each result binds the advertised
resolution-contract SHA-256 and publishes only the verified receipt SHA-256.
Raw receipts, credentials, reviewer prose, qualified-human identity, source
and target text, project prices and project brands remain excluded. The
release-evidence schema uses the same provider-neutral resolution schema, so a
consumer does not need to infer these conditional rules from documentation.

Every commercial locale profile also carries an exact Unicode CLDR 48 numbers
reference. Use its resolved locale, numbering system, grouping threshold,
symbols, and standard decimal, percent, currency, ISO-currency, approximation,
limit, and range patterns as native rendering guidance. Do not use a surface
pattern or cross-language regex as evidence that the value is semantically
equal. Meaning-preserving number words, written percentages, and digit forms
remain eligible; unresolved values require independent model or qualified
native-domain review.
The worker uses the existing three ordered passes; the source-fidelity response
additionally requires `commercial_review`. It contains all ten named dimensions,
coverage, a canonical offer registry, and per-offer evidence. Each registry
entry has a unique stable identifier plus ordered, non-overlapping source and
target regions. Multiple discontiguous regions are allowed for linked footnotes
and conditions, but regions cannot overlap across offers. Each evidence item
declares `matched`, `source_only`, or `target_only`, names a registered offer,
and supplies exact source/target character spans contained in that offer's
declared regions; the absent side of a one-sided item is `null`. Equivalent
evidence must be matched, and `offer_assignment` must name every registered
offer exactly once. A specific changed or uncertain verdict needs a concrete
item, while globally uncertain coverage may remain span-free rather than
fabricate a location.
Missing/malformed checks, impossible relation/span combinations and changed
terms produce no result or approval. Uncertainty or an all-absent report preserves the candidate only as a
low-confidence fidelity result. It remains unpublishable until the host verifies
exactly one qualified native/domain review or an independent second-provider
model receipt bound to the commercial profile and policy. The worker does not
secretly call an alternative provider.

The worker retains a content-free `commercial_review` summary in result schema
v8. It contains only the profile, verdict, exact review-evidence-contract
SHA-256, ordered unresolved dimension names
and a hash binding the profile, exact source and target hashes, and complete
commercial evidence. Prices, text spans, interpretations and reviewer prose do
not survive in the summary. The same
summary is bound into quality-evidence request schema v12 and receipt-binding
schema v8, so an independent adapter receives the exact targeted scope and
cannot replace it with a generic approval. Tampered, unknown, reordered or
contradictory dimensions block before network access or signing.

For a review-required result, the worker also derives the private
`translate-native.commercial-review-routing.v2` context from the already
validated offer registry. It maps each zero-based opaque offer index to ordered
source and target Unicode code-point spans and binds both text lengths. The
context deliberately strips configured IDs, text, prices, brands, and reviewer
explanations. It carries the exact digest of the separately advertised,
machine-readable routing contract, which fixes code-point offsets, exclusive
ends, complete text lengths, registry-order coverage, overlap rules, and the
private-only trust boundary. Evidence-request identity and every receipt
binding cover the route and contract digest;
span, order, count, or length drift blocks before network access. It never
appears in public CMS release evidence.
For unresolved work, the evidence request also includes the complete
content-free routing contract. The coordinator and HTTPS adapter independently
reconstruct it, and request identity covers the exact object. Verified
commercial and non-commercial requests require both route and contract to be
`null`.

The validator checks evidence shape and offsets, **not semantic truth**. A model
can misinterpret text or omit a fact while claiming completeness. The host's
independent quality-receipt verifier must inspect the complete request/response
and adequacy of evidence before signing; existing signature/readiness gates
remain mandatory. No live provider or native-editor quality is established by
scripted adapter tests. Profile changes require a new profile version, and
project condition/glossary/prompt changes require new bound policy versions.

The website benchmark suite v6 treats commercial localization as one of eight
separately gated content-type lanes. Each locale needs all eight brand-neutral
commercial cases and must pass the predeclared joint, target-native, and
source-fidelity statistics inside that lane. Wins on headlines, UI, or other
content cannot compensate for a weak pricing lane, just as commercial wins
cannot hide another weak content type. These gates prove only the integrity of
recorded blind evidence; they do not manufacture native review or establish
superiority without real qualified reviewers and a lawful baseline.

Its hashed manifest binds the same ten ordered dimensions as this profile to
every commercial case. This includes dimensions absent from the source, because
the target must also be checked for invented prices, discounts, terms, or
conditions. The runner validates the exact list before either external pass and
provides it only to source-fidelity review. Source-blind native review receives
neither the source nor this semantic scope. Missing, additional, or reordered
dimensions fail closed before a reviewer is called.

Each commercial case also carries a manually versioned
`translate-native.commercial-benchmark-offer-registry.v1`. It partitions the
complete source into ordered Unicode-code-point spans assigned to opaque offer
indexes and explicitly shared spans. Exact source length, registry content and
SHA-256 travel only with source-fidelity review. Gaps, overlaps, reordered
indexes, byte-offset substitutions or a stale digest fail before external
review. Shared conditions must be considered for every relevant offer; the
reviewer may not infer a different source partition.

The fidelity reviewer must return
`translate-native.commercial-benchmark-review.v3`: one ordered item for every
dimension and, within each anonymous variant, one ordered status for every
opaque offer index registered by the fixture. `equivalent` and `not_present`
carry no defect reference. `major` and `blocking` point to the matching
variant's zero-based defect entry. The dimension aggregate is derived by fixed
severity and cannot hide a defective offer. `uncertain`, missing, duplicated or
reordered offers, additional rows and contradictory aggregates fail closed.
Signed case evidence retains the complete per-offer status matrix and exact
registry digest with canonical response hashes and defect counts; reviewer
prose and source spans do not survive.
