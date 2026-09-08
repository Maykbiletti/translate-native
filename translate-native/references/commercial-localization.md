# Commercial localization — profile v1

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

## Website worker contract

The trusted CMS/backend selects `content_type: "commercial"` for complete offer
copy, including its CTA and conditions. Do not let a generating agent downgrade
it to `marketing` or `ui` to avoid review. Keep genuinely legal terms on the
existing `legal` path with required human review; this profile is not a legal
approval. For mixed pages, supply complete contextual commercial units rather
than isolated price fragments.

The planner binds `translate-native.commercial.v1` into the job and plan IDs.
The worker uses the existing three ordered passes; the source-fidelity response
additionally requires `commercial_review`. It contains all ten named dimensions,
coverage and per-offer evidence with exact source/target character spans.
Missing/malformed checks and changed terms produce no result or approval.
Uncertainty or an all-absent report preserves the candidate only as a
low-confidence fidelity result. It remains unpublishable until the host verifies
exactly one qualified native/domain review or an independent second-provider
model receipt bound to the commercial profile and policy. The worker does not
secretly call an alternative provider.

The validator checks evidence shape and offsets, **not semantic truth**. A model
can misinterpret text or omit a fact while claiming completeness. The host's
independent quality-receipt verifier must inspect the complete request/response
and adequacy of evidence before signing; existing signature/readiness gates
remain mandatory. No live provider or native-editor quality is established by
scripted adapter tests. Profile changes require a new profile version, and
project condition/glossary/prompt changes require new bound policy versions.
