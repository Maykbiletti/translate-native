# Website localization job planning

`integrations/website_localization.py` is the provider-neutral entry point for
the website-localization pipeline. It does not contact an LLM and cannot
publish content. Its only job is to turn one trusted source object into one
independently retryable queue payload per EU target locale.

## Premortem

Assume the planner shipped and failed: stale work was reused after a source,
glossary, policy, provider, model, or runtime change; the source language was
translated back into itself; or one queue entry mixed several target
languages. The early warning is a repeated job identity despite one changed
input or a job count that differs from 23 for an EU source and 24 for a non-EU
source. The mitigation is one immutable job per locale whose canonical
idempotency binding includes every result-affecting field and the exact source
hash. The regression suite mutates each binding separately and proves the
expected 24-profile registry and per-language job counts.

The current language registry follows the European Union's official list of 24
languages: <https://european-union.europa.eu/principles-countries-history/languages_en>.
Each language has one explicit BCP-47 website profile. The default German
profile is `de-AT`; English is `en-IE`, Portuguese is `pt-PT`, Spanish is
`es-ES`, and Swedish is `sv-SE`. A source whose primary language is already in
the registry produces 23 jobs. A non-EU source produces all 24.

`integrations/website_localization_quality_profiles.py` adds one immutable,
versioned evaluation profile for each of those 24 locales. Each profile has
separate target-only nativeness criteria, source-aware fidelity criteria, and
locale-specific adversarial cases. Every profile also requires the complete
red-team matrix for translationese, wrong neighbouring language, mixed
varieties, ASCII folding, missing diacritics or native script, wrong
inflection, omitted meaning, unnatural CTAs, and marketing calques. This
shared minimum does not replace the language-specific criteria.

The canonical profile hash and version are part of the target profile and job
identity. The complete profile is supplied independently to transcreation,
target-only review, source-aware review, and blinded benchmark review. Worker
results carry the locale, version, and hash; quality-evidence requests and
signed approvals bind that triplet again. A missing, substituted, or stale
profile therefore blocks before a provider call or release instead of falling
back to generic instructions.

Finnish criteria explicitly cover natural information structure, case
government, agglutination, possessive suffixes, vowel harmony, consonant
gradation, clitics, compounds, politeness, and non-calqued web CTAs. Maltese
criteria cover `ċ`, `ġ`, `għ`, `ħ`, and `ż`, morphology, fused articles and
prepositions, idiom, and English/Italian calques. The Maltese institutional
reference is the [Kunsill Nazzjonali tal-Ilsien Malti](https://kunsilltalmalti.gov.mt/mistoqssija-u-twegiba-51-76/);
locale exemplar and convention references are pinned to
[Unicode CLDR 48](https://www.unicode.org/cldr/charts/48/summary/mt.html).

## JSON contract

```bash
python3 integrations/website_localization.py <<'JSON'
{
  "source_id": "homepage.hero",
  "source_revision": "cms-184",
  "source_text": "Build your business with BLUN.",
  "source_locale": "en-IE",
  "content_type": "headline",
  "glossary_version": "blun-glossary-3",
  "policy_version": "native-web-1",
  "provider_id": "customer-llm",
  "model_id": "king",
  "model_version": "2026-08-29",
  "software_version": "6.43.0-dev",
  "target_locales": ["de-AT", "sv-SE"]
}
JSON
```

Omit `target_locales` to request every eligible EU language. An explicit list
must contain supported profiles, must not contain duplicates, and must exclude
the source language. Unknown input fields, ambiguous locale values such as
`auto`, non-NFC text, NUL characters, unsupported content types, oversized
source text, and wrong JSON types block the complete plan.

Every emitted job contains exactly one target profile, the unchanged source
text and its SHA-256 hash, both required quality-pass names, and
`release_required: true`. Its `job_id` and `idempotency_key` are identical and
are derived from canonical JSON bound to:

- source ID, revision, text hash, and locale;
- complete target-locale metadata, including quality-profile version and hash,
  plus content type;
- glossary and quality-policy versions;
- provider, model ID, and model version;
- Translate Native software version.

Changing any bound value creates a new job and plan identity. A queue may
therefore deduplicate an exact retry, while stale work cannot silently survive
a changed source, glossary, policy, provider, model, or runtime. Later workers
must keep the two declared reviews separate—target-only native quality first,
source-aware fidelity second—and obtain a signed Translate Native release
before publication.

## Durable queue state

`integrations/website_localization_queue.py` persists planner jobs through a
trusted host-supplied `sqlite3.Connection`. Enqueuing a complete plan is one
transaction: an exact repeat inserts nothing, while a reused job ID with
different bytes rolls the entire operation back. Workers claim one locale at a
time through a random, owner-bound lease. Lease expiry recovers work after a
crash, but stale claims cannot acknowledge a newer attempt.

The queue records `pending`, `leased`, `retry_wait`, `succeeded`, and `failed`
states, bounded attempt counts, the next eligible attempt time, result hashes,
and stable error codes. Free-form error detail is represented only by a
SHA-256 hash so status inspection does not disclose customer prose. Payloads
are hashed on insertion and checked again before a worker receives them.

Queue `succeeded` means only that a worker returned finite, NFC JSON. It is not
a native-quality attestation, signed release, or publication permission. The
later review and release stages must still perform the ordered target-only and
source-aware checks and verify a purpose-bound Translate Native receipt.

Premortem: a worker may crash while leased, retry forever, replay a stale
claim, or collide with different content under the same idempotency key. The
lease token changes on every attempt, expiry consumes the abandoned attempt,
the configured attempt ceiling becomes terminal, and every collision or
payload-integrity failure blocks transactionally. Cross-connection and crash
recovery regressions prove those boundaries.

## Provider-neutral worker contract

`integrations/website_localization_worker.py` consumes exactly one locale job
and calls a host-supplied adapter implementing `invoke(ProviderRequest)`. The
contract contains no BLUN.ai, OpenAI, Anthropic, or other provider-specific
transport. An adapter maps the immutable request to its own API and either
returns strict JSON or raises the content-free `ProviderCallFailed` with a
stable error code and retryability decision.

Every worker attempt has three ordered calls:

1. `transcreation` receives the complete source, one exact BCP-47 target,
   content-specific guidance, and the resolved glossary, audience, tone, and
   protected terms;
2. `target_native` receives only the candidate and target-side terminology—no
   source text, source locale, or source glossary terms—and rejects unnatural
   wording, translationese, register, script, orthography, and locale errors;
3. `source_fidelity` runs only after the native review passes and checks the
   candidate against the complete source for meaning, completeness,
   terminology, and protected syntax.

The trusted host resolves `LocalizationAssets` from immutable registries. Its
glossary and policy versions must exactly match the versions already bound to
the job; stale assets block before any provider call. The policy version owns
the audience, tone profile, prompt rules, and review standard. Provider
responses must use the exact phase, locale, and schema, contain no extra
fields, and use NFC text. Each review must also report `confidence` as exactly
`high` or `low`. Missing or unknown confidence is malformed and blocks; `high`
does not replace either substantive review, while `low` adds a mandatory
independent-review requirement to the result. A wrong locale, malformed
response, failed review, provider exception, or changed job binding blocks
without producing a queue result.

After both LLM reviews pass, the bundled local translation guard independently
checks Unicode NFC, HTML/JSON/XML structure, placeholders, links, code,
protected tokens, untranslated segments, and major omissions. Worker results
bind source and target hashes, locales, content type, glossary and policy
versions, provider/model identity, software version, and hashes of all three
requests and responses. The explicit per-phase confidence decision is carried
in the result and remains bound through quality evidence and the signed
approval. Results retain no reviewer prose and still set
`release_required: true`; queue success therefore remains neither a signed
release nor publication permission. Legal content sets
`human_review_required: true`; low confidence in either review instead sets
`independent_review_required: true` for non-legal content. The latter can be
satisfied only by a separately verified qualified-human receipt or a verified
second model adapter with a different provider identity.

Premortem: a provider could answer in the wrong locale, merge creation and
review, leak the source into the native-only judgment, return convincing but
unstructured prose, or pass a candidate with a broken placeholder. Exact
phase and locale schemas, separate inputs, ordered calls, version matching,
response hashes, and the final local integrity gate make each case fail closed.
The same profile version and hash are carried into the unsigned worker result,
then into external quality evidence and the signed approval, so a later profile
change cannot reuse an older translation-memory entry.

## Queue-to-worker execution

`integrations/website_localization_runner.py` is the narrow bridge between the
durable queue and the provider-neutral worker. One call claims at most one
locale, resolves its exact provider and versioned assets through host-supplied
callbacks, executes the three quality stages, and performs one lease-bound
queue transition. A successful transition stores the unsigned worker result;
it still cannot publish, replace a last-known-good translation, or make the
overall website version ready.

The runner renews the lease after dependency resolution and after every
validated worker phase. If a provider call outlives the lease, a stale worker
cannot record its output. Retryable failures use deterministic bounded
exponential backoff and become terminal at the job's attempt ceiling.
Non-retryable failures stop only that locale. Status output contains stable
codes and opaque finding hashes, never provider exceptions, reviewer prose, or
candidate text. Mixed success and failure therefore remains visible per locale
while publication stays blocked until a later release coordinator verifies all
required signed approvals.

Premortem: the bridge could acknowledge output after losing its lease, retry a
permanent configuration error forever, expose source text through exception
messages, or let one failed locale erase a successful sibling. Exact lease
tokens guard every transition; typed dependency and worker failures preserve
retryability without prose; attempts are bounded; and each invocation mutates
only its claimed locale. Regression tests exercise lease expiry, retry
exhaustion, opaque errors, and partial provider failure.

### Signed local fallback

The low-level runner's optional `result_cache` is deliberately consulted before
asset or provider resolution. The complete production runtime does not accept a
host-supplied cache: it always constructs
`LocalizationReleaseStore.verified_result_cache(authority)` from its own local
signed release store and the already validated approval authority. That adapter
loads the exact deterministic job from the local translation memory, rechecks
the stored result hash, approval payload hash, complete job binding, approval
ID, signing-key identity, signature, and expiry, and returns the already
reviewed worker result only when every check passes. `RunOutcome.result_origin`
then reports `translation_memory`; a new provider result reports `provider`.

A missing or expired approval is a clean cache miss. The normal provider path
must produce and review a new target, so an unavailable model still leaves the
locale blocked without inventing text. Any malformed, altered, or unverifiable
cache entry blocks that attempt before provider or asset lookup and follows the
queue's bounded retry policy. Because job identity binds source, locale,
content type, glossary, policy, provider/model, worker schema, and software
version, changing any of them cannot reuse an older translation.

Premortem: an offline deployment could mistake an expired translation for a
safe fallback, silently use a signature from another source or policy, or call
the provider after discovering local tampering. The adapter treats expiry as a
miss, verifies every signed binding before returning content, and makes cache
verification errors stop the attempt before any other resolver runs. Tests
cover exact offline recovery, policy invalidation, expiry, and database
tampering across separate queue and translation-memory connections.

## Blind quality benchmark against an external baseline

`integrations/website_localization_benchmark.py` provides the evidence gate for
quality claims such as “better than DeepL.” The module does not call DeepL or
any other baseline service. A host may create a comparison artifact only from
the provider's official API or a lawfully supplied fixed fixture. It calls
`create_baseline_artifact` with the exact target and a provenance record whose
method is `official_api` or `lawful_fixture`, plus a stable evidence identifier
and SHA-256 digest of the host-retained acquisition record. Undocumented
endpoints and scraping are deliberately not valid provenance methods.

The artifact binds the baseline identity and version, exact source hash,
target locale, content type, target hash, complete target text, and provenance.
The same host-owned `BenchmarkEvidenceAuthority` attests those canonical bytes
and immediately verifies its own result. The harness rejects unsigned,
foreign-key, changed, or malformed baseline evidence before either reviewer is
called. The case result retains only target, provenance, and complete artifact
hashes; the final report includes a digest over the exact baseline-evidence set.
Neither provenance nor baseline identity enters a reviewer request. A signature
proves integrity and host approval, not that a false provenance statement is
legally true, so the host must preserve the API receipt or fixture licence for
audit. Credentials and transport code do not belong in benchmark artifacts.

Every policy-required locale and source case also requires one versioned native
reference artifact before the first blind review can run. The repository does
not ship or invent reference translations. A host obtains the exact
`native_reference_verification_request`, including the complete source and its
hash, suite case, target locale, content type, glossary and localization-policy
versions, locale-profile version and hash, complete reference target, and
reference revision. A configured `NativeReferenceVerifier` must
then validate an opaque receipt from a separately identified qualified native
human reviewer. Candidate provider, baseline, A/B reviewer, reference verifier,
and reference reviewer identities must remain distinct.

`create_native_reference_artifact` verifies that receipt before a host-owned
`BenchmarkEvidenceAuthority` attests the complete artifact. The benchmark
rechecks both the attestation and qualified-review receipt immediately before
review and binds the complete evidence hash into the keyed blind assignment,
case result, and final report. A receipt therefore cannot be replayed across a
source case, source text, locale, content type, glossary, localization policy,
quality profile, reference revision, or reviewer credential. Missing, altered,
rejected, or contradictory reference evidence blocks without calling the A/B
reviewer.

Reference text, reviewer identity, and receipt never enter either A/B request
or the text-free case result; only hashes and the public reference revision are
retained. This preserves the strictly ordered two-stage decision: source-blind
native quality first, then source-aware fidelity. The reference is auditable
eligibility and calibration evidence, not a hidden third score and not proof
that a model is linguistically superior. The test references are synthetic
contract fixtures only and make no native-quality claim.

Every benchmark policy must bind the exact version and SHA-256 digest of the
output-free source manifest in
`integrations/website_localization_benchmark_suite.py`. Its eight cases cover
all supported content types across distinct domains, include two connected
long-form pages, and exercise HTML, JSON, placeholders, links, negation,
modality, commercial offers, amounts, currencies, discount basis, tax,
billing interval, contract term, renewal, and cancellation. The suite contains
no target, candidate, baseline, or supposed reference translation. Actual
targets must still come from the attached candidate and lawfully acquired
baseline so unreviewed prose cannot silently become a gold standard.

Early locale lanes may be run and reported independently, but passing them no
longer authorizes an EU-wide superiority statement. The attested report exposes
`configured_lanes_status` separately from `superiority_claim_allowed` and
includes an exact `claim_scope` with required, evaluated, missing, unexpected,
and source-language locales. A public claim is allowed only when the configured
locale set exactly covers every EU target eligible for the bound source suite
and every one of those locale reports passes on its own. One missing or blocked
locale therefore blocks the overall report; no aggregate can conceal it.

Each locale report also exposes separate statistics for `target_native` and
`source_fidelity`: candidate wins, baseline wins, ties, decisive rate, candidate
win rate, and the one-sided sign-test probability. Both axes must independently
meet the policy's predeclared sample, decisiveness, win-rate, and significance
thresholds. Those thresholds and fixed block reasons are included in the
attested report. Joint case winners remain an additional conservative metric,
but discarded cross-axis disagreements can no longer make a weak axis appear
statistically convincing.

The current suite source language is English, so its EU localization target
scope contains the other 23 official-language locale profiles. `en-IE` is
recorded explicitly as the source-language locale and is not sent through a
same-language translation job, matching the planner's source-exclusion rule.
This report does not claim that English localization from a non-English source
was evaluated. A future suite revision with a different source-language design
must change the bound suite digest and will derive a new exact claim scope.

Each case compares one fully validated worker result with one bound baseline
artifact. A host-held blinding key assigns them reproducibly to anonymous `A`
and `B` positions. Neither reviewer request contains candidate-provider or
baseline identity. The first review receives only the two targets, the exact
locale profile, audience, tone, target terminology, and content type. The
second review receives the complete source and glossary for a separate
fidelity judgment. Both must prefer the same variant; a preferred variant with
any blocking or major defect is an invalid review. The local structure guard
independently checks both variants. Raw texts and reviewer prose are absent
from stored case results; only hashes, counts, blinded commitments, and
unblinded preferences remain.

The policy also names the exact candidate provider, model and model version,
software version, glossary version, localization-policy version, and worker
schema. The harness rejects a job that differs from any of these values before
calling a reviewer. Stored case results repeat this candidate binding and the
exact locale-quality-profile version and hash. During aggregation, the harness
rebuilds the canonical suite job and requires its deterministic job ID, so a
result from another model, policy, source case, or locale cannot be relabelled.
The final report records the same candidate binding and all required locale
profile bindings. These fields never enter either blinded reviewer request.

Benchmark results are durable evidence only when a host-owned
`BenchmarkEvidenceAuthority` attests them. The harness passes canonical UTF-8
bytes to that provider-neutral interface and never reads a signing key. The
policy fixes the expected algorithm and key identifier. After both blind
reviews, the harness signs and immediately verifies the complete text-free
case result; missing, rejected, foreign-key, or payload-mismatched attestations
block before aggregation. `summarize_benchmark` verifies every case first,
binds the report to the digest of the exact signed case set, and attests the
complete report. Consumers can call `verify_benchmark_report` with that exact
case evidence before accepting even a `PASS` claim. Production hosts should
back the authority with an isolated signer or hardware-backed key and restrict
it to this benchmark contract; the test-only HMAC authority is not production
key management.

`summarize_benchmark` applies a one-sided exact sign test and minimum case,
decisive-rate, and win-rate thresholds separately to every required locale.
One candidate blocking/major/integrity defect blocks that locale. Missing,
small, tied, mixed-version, substituted-suite, or duplicate samples block the
superiority claim, and a strong result in one language can never average away
a weak result in another. Maltesisch (`mt-MT`) and Finnisch (`fi-FI`) are the
initial mandatory lanes and cannot be removed from policy. A locale passes only after every
canonical suite case is present exactly once; the report records observed
content types, domains, long-form count, and adversarial tags. The same
versioned contract extends to every eligible EU language profile, excluding
the source language as required by the localization planner.

Premortem: reviewers could learn which output came from which system, a large
language could hide a weak low-resource language, an arbitrary output could be
labelled as an official baseline, or an old baseline could be quietly reused.
Attested lawful provenance, keyed A/B assignment, and origin-free review payloads reduce
identity bias; per-locale hard gates prevent averaging; exact baseline,
reviewer, benchmark, suite, suite case, source, locale, and content bindings
plus exact candidate and quality-profile bindings reject stale, substituted,
homogeneous, relabelled, unsigned, forged, or mixed evidence. The harness permits
a claim only from complete measured blind evidence, never from a model grading
its own prose.

## Signed translation memory and website readiness

`integrations/website_localization_release.py` turns a completed queue result
into an append-only translation-memory entry only after a host-owned verifier
accepts a quality receipt for the exact source, target, and locale. The module
never reads a signing key. Instead, a trusted `ApprovalAuthority` signs and
immediately verifies the canonical approval bytes outside the worker's
authority. Production hosts should implement that interface with an isolated
service or hardware-backed signer; the repository tests use HMAC only as a
deterministic test double.

Every approval binds the exact source and target hashes, source and target
locales, content type, glossary and policy versions, provider/model identity,
worker schema, software version, queue-result hash, quality-receipt hash,
the explicit two-phase review confidence, approval lifetime, and signing-key
identity. Legal content additionally needs a separately verified qualified-human
receipt. For non-legal content, low native or fidelity confidence requires
exactly one separately verified qualified-human receipt or an independent
second-provider model receipt. Raw receipts are never stored.
Approvals for one deterministic job may be reused across different plan
compositions, but a changed source, policy, glossary, provider, model, or
software version produces a different job and therefore a cache miss. An
already approved job can never be overwritten with a different target hash.

Before any publication adapter receives content, `readiness` revalidates every
stored result, approval payload, expiry, and signature for the plan's exact
required locale set. `publication_bundle` returns content only when all
required locales pass; a missing, expired, altered, or invalid approval blocks
the whole website version without deleting an older known-good entry. The
release store uses its own trusted host-supplied SQLite connection, separate
from the queue connection, and performs no network or publication action.

Premortem: a signature might be replayed after policy drift, a database edit
might swap the target, or a partial rollout might be mistaken for completion.
The deterministic job binding invalidates drift, append-only target hashes and
canonical payload signatures expose tampering, and readiness requires exact
set equality across all policy-required locales. Tests cover source, policy,
model and software invalidation, signature/result corruption, expiry, legal
review, partial readiness, and cross-plan translation-memory reuse.

## Quality evidence and release coordination

`integrations/website_localization_release_coordinator.py` joins completed
locale jobs, external quality evidence, signed translation memory, and the CMS
outbox without embedding a model or reviewer. Each invocation approves at
most one completed locale. It creates a CMS delivery only after the release
store independently revalidates every locale required by the plan. Pending,
retrying, or terminally failed siblings therefore cannot leak a partial
website version into the publication outbox.

The host supplies a `QualityEvidenceProvider` implementing
`obtain(QualityEvidenceRequest)`. One request contains exactly one complete
source and target, plus their hashes, the CMS event, plan and job identities,
source and target locales, content type, glossary and policy versions,
provider/model identity, software version, and a host-chosen
`evidence_revision`. Its deterministic `request_id` binds all non-text fields
and the exact validated queue-result hash. The adapter may call an independent
model, a qualified native reviewer, or a host-owned review service; no
provider transport or credential is built into the coordinator.

The host must also supply a `QualityEvidenceStateStore` backed by its own
trusted SQLite connection and a stable `evidence_worker_id`. Before source or
target text reaches the evidence adapter, the store atomically claims the
exact request through an owner- and token-bound lease. A second coordinator
cannot call the provider while that lease is live. Lease expiry recovers an
abandoned attempt after a crash; stale workers cannot finish a newer claim.
Retryable failures use bounded exponential backoff and the configured attempt
ceiling, while permanent evidence or receipt failures stop immediately.

The adapter must return exactly this shape:

```json
{
  "schema": "blun.localization-quality-evidence-response.v2",
  "request_id": "blun-l10n-evidence-…",
  "result_sha256": "…",
  "quality_receipt": "host-verifiable-purpose-bound-receipt",
  "human_review_receipt": null,
  "independent_model_review": null
}
```

The response is rejected if the request object was mutated, a binding differs,
the receipt is empty or malformed, or the trusted quality verifier rejects it.
The evidence request binds the explicit native and fidelity confidence values.
Legal content always requires a non-null human receipt and a separate
human-review verifier. For non-legal content with low confidence, the adapter
must return exactly one of that qualified-human receipt or an independent model
review in this form:

```json
{
  "schema": "blun.independent-model-review.v1",
  "provider": {
    "id": "independent-provider",
    "model_id": "configured-review-model",
    "model_version": "immutable-model-version"
  },
  "receipt": "host-verifiable-purpose-bound-receipt"
}
```

The independent reviewer must use a different provider adapter identity from
the primary translation provider. The host supplies a separate verifier; its
verified receipt hash and exact provider/model identity are included in the
signed approval. Missing, ambiguous, same-provider, malformed, or rejected
evidence keeps the locale blocked. A receipt is evidence for the existing two
ordered reviews—first source-blind native quality, then source-aware
fidelity—not permission to collapse them into one score. A major defect still
blocks the worker result entirely; it cannot be outweighed or converted into a
confidence decision.

Exact retries are safe: approved locales are reused, the outbox has a stable
delivery identity, and a crash after signing an approval but before recording
evidence completion is reconciled from that verified approval without another
provider call. The deterministic `request_id` remains the adapter's external
idempotency key for the narrow crash window after a provider accepts a request
but before the local attempt is durably finished. An expired partial approval
requires new evidence and a new `evidence_revision`. A pending delivery with
expired approvals is blocked; a previously acknowledged delivery remains
immutable terminal history.

`statuses(event_id)` exposes the evidence state, attempt count, retry time,
lease expiry, and stable last-error code for monitoring. It stores and returns
no source text, target text, receipt, provider prose, or credential.
Coordinator outcomes and exceptions follow the same content-free rule.

Premortem: two schedulers could request the same review, stale evidence could
approve changed output, or the last successful locale could trigger a partial
publication. Transactional leases prevent concurrent provider calls;
deterministic evidence IDs cover the remaining external crash window; exact
result and policy bindings reject stale evidence; signed release readiness and
the all-locale CMS transaction block partial publication. Tests cover
exclusive claims, bounded retries, one-locale progression, replay, crash
recovery, expiry, legal review, tampering, provider failure, wrong bindings,
and failed receipt verification.
They prove the orchestration boundary, not native linguistic quality or
superiority over an external translation service.

## CMS change and publication contract

`integrations/website_localization_cms.py` connects the pipeline to a CMS
without choosing a vendor or network library. The host supplies three isolated
capabilities: an inbound signature verifier, an outbound signing authority,
and a publisher implementing `publish(CMSPublicationRequest)`. The bridge does
not read keys, choose credentials, or update live CMS state by itself. A host
can inject its own publisher or use the included provider-neutral HTTPS
publisher described below.

An inbound `blun.cms-content-change.v2` event has exactly these fields:

```json
{
  "schema": "blun.cms-content-change.v2",
  "event_id": "cms-event-184",
  "site_id": "blun-marketing",
  "website_version": "website-2026-08-29.1",
  "source_sequence": 184,
  "localization": {
    "source_id": "homepage.hero",
    "source_revision": "cms-184",
    "source_text": "Build your business with BLUN.",
    "source_locale": "en-IE",
    "content_type": "headline",
    "glossary_version": "blun-glossary-3",
    "policy_version": "native-web-1",
    "provider_id": "customer-llm",
    "model_id": "king",
    "model_version": "2026-08-29",
    "software_version": "6.43.0-dev",
    "target_locales": ["de-AT", "sv-SE"]
  }
}
```

The CMS signs the canonical UTF-8 JSON bytes outside the envelope. The bridge
verifies the signature before its first write, derives the deterministic plan,
and persists the event before enqueuing it. If the process stops between those
two transactions, replaying the exact event resumes queue insertion safely.
The same `event_id` with different canonical bytes is an idempotency collision
and cannot add work.

For each `(site_id, source_id)`, the CMS supplies a positive, monotonic
`source_sequence` inside the signed event. Once a newer event has reached
`enqueued`, every older, not-yet-
published generation of that source is marked superseded. Exact replay keeps
its signed sequence and therefore cannot displace a newer event; a delayed,
out-of-order webhook with a lower sequence is superseded immediately. Reusing
one sequence for a different event is an idempotency collision. A
superseded event cannot create a release; any pending, retrying, or leased
outbox entry becomes terminal with the content-free `event_superseded` reason.
The service passes only current plan IDs into the queue claim, so a locale job
referenced exclusively by superseded plans is never sent to a model. A
content-identical job still remains eligible when any current plan references
it.

New events must use the v2 contract. During the transactional database-v1
migration, already stored v1 events receive deterministic legacy generations;
afterward only an exact, signed replay of such a stored event is accepted so a
crash between persistence and queue insertion can still resume. A new v1 event
is rejected rather than entering an ordering domain without a signed sequence.
Independent sites and source IDs remain independent, and an already accepted
publication remains immutable history. Schema-v1 databases migrate these
generations and supersessions transactionally before normal operation resumes.

After every required locale has a valid signed approval, `prepare_delivery`
creates one `blun.cms-localization-publication.v2` payload for the complete
locale set. It includes the site and website version, source identity, signed
source sequence and hash,
and, for each locale, the exact target text and hash, approval ID, and expiry.
Its deterministic `delivery_id` is an idempotency key over those immutable
bytes. The host-owned publication authority signs and immediately verifies the
payload before the durable outbox accepts it. A partial, changed, expired, or
invalid approval creates no publication entry.

Outbox workers claim a delivery through an owner- and token-bound lease. The
publisher must return exactly:

```json
{
  "schema": "blun.cms-localization-publication-ack.v1",
  "delivery_id": "blun-cms-delivery-…",
  "payload_sha256": "…",
  "status": "accepted"
}
```

`integrations/website_localization_cms_http.py` is the concrete HTTP publisher
for this contract. Its endpoint is trusted deployment configuration and must
not be derived from an inbound event. It requires HTTPS; plain HTTP is
available only through an explicit loopback-only development option. The
adapter disables redirects, obtains authentication headers from a callback for
each attempt, prevents that callback from replacing protocol headers, sends
`Accept-Encoding: identity`, and bounds both timeout and response size. URL
credentials, query-string secrets, control characters and ambiguous duplicate
critical headers are rejected before acceptance.

The request body contains the exact signed publication rather than another
translation format:

```json
{
  "schema": "blun.cms-localization-publication-http.v1",
  "payload_sha256": "…",
  "publication": { "schema": "blun.cms-localization-publication.v2" },
  "signature": {
    "algorithm": "ed25519",
    "key_id": "publisher-2026-09",
    "signature": "…"
  }
}
```

The complete publication object remains nested in `publication`. The adapter
also sends the immutable `delivery_id` as `Idempotency-Key` and repeats the
delivery ID and payload hash in reserved binding headers. The CMS must verify
the publication signature and hash before its atomic source-revision write.

HTTP success alone is insufficient. Status 200 must contain a strictly parsed,
UTF-8 JSON envelope with the exact acknowledgement above and a CMS signature
over its canonical bytes:

```json
{
  "schema": "blun.cms-localization-publication-http-ack.v1",
  "acknowledgement": {
    "schema": "blun.cms-localization-publication-ack.v1",
    "delivery_id": "blun-cms-delivery-…",
    "payload_sha256": "…",
    "status": "accepted"
  },
  "signature": {
    "algorithm": "ed25519",
    "key_id": "cms-2026-09",
    "signature": "…"
  }
}
```

The acknowledgement verifier is independent from the publication signer.
Redirects and other 3xx responses are terminal. HTTP 408, 425, 429 and 5xx
responses, network failures and malformed response transport are retryable
under the existing bounded outbox policy; other non-200 statuses, wrong
bindings and invalid signatures are terminal. No response body, credential or
exception detail enters the durable status record.

Wrong or malformed acknowledgements retry with bounded exponential backoff;
explicit permanent rejections become terminal. Crashed leases are recovered,
but stale workers cannot acknowledge a later attempt. Free-form transport
details are stored only as SHA-256 hashes. Delivery is at least once, so a CMS
adapter must make the stable `delivery_id` idempotent: acceptance followed by a
crash may send the exact same signed payload again. A failed new website
version never deletes or overwrites an older successful delivery.

There is an unavoidable boundary after a publisher begins an external request:
local code cannot retract bytes already received by a CMS. The receiving CMS
must therefore compare `(site_id, source_id, source_sequence, source_revision,
source_sha256)` atomically with its current source revision and reject a stale
payload even if its signature is otherwise valid. It must acknowledge
`accepted` only after
that conditional write succeeds. This complements the local generation gate
and closes the lease-to-network race without requiring a vendor-specific API.

Premortem: an attacker could reuse an event ID with changed content, a partial
locale set could reach the CMS, an acknowledgement could name another payload,
an older event could finish after a newer source revision, or a worker could
wake after its lease or approval expired. Canonical inbound
signatures and collision checks block changed events; the release gate creates
only complete bundles; monotonic source generations and the receiver-side
revision comparison block stale publication; exact signed payload hashes bind
acknowledgements; and both leases and approval expiries are rechecked
immediately before delivery. Regression tests cover replay, collision,
supersession, migration, partial readiness, tampering, exact acknowledgements,
bounded retries, opaque failures, and crash recovery.

## Commercial price and offer profile

Select `content_type: "commercial"` in the trusted CMS/backend for pricing,
offers, subscriptions and their contextual CTAs/conditions. This adds the
versioned `translate-native.commercial.v2` profile to the job payload, job ID
and plan ID; the existing seven types retain their previous payloads and IDs.
It is available for every planner locale, including `mt-MT` and `fi-FI`.
The public skill's [commercial guide](../translate-native/references/commercial-localization.md)
applies to all languages, with no hardcoded project prices, brands or products.

The three provider calls stay ordered: transcreation, source-hidden native
editing, source-aware fidelity. Commercial fidelity additionally returns
`commercial_review` with the profile schema, `coverage` (`complete` or
`uncertain`), and `checks` for `amount_currency`, `discount_basis`, `qualifiers`,
`tax_status`, `billing_interval`, `commitment`, `renewal`, `cancellation`,
`conditions`, and `offer_assignment`. Every check has `status` (`equivalent`,
`not_present`, `changed`, `uncertain`) and `items`; each item has `offer`,
`relation` (`matched`, `source_only`, or `target_only`), `source_span`,
`target_span`, and `explanation`. Spans are zero-based Unicode code-point
offsets with an exclusive end. A one-sided item uses `null` only for the side
that is absent, so an omitted condition and an invented target claim can be
represented without fabricating a counterpart. The exact response contract and
dimension guidance are supplied in each fidelity request.

Equivalent checks require matched evidence; absent dimensions require empty
items. A dimension-level changed or uncertain verdict requires at least one
specific evidence item, while globally uncertain coverage may remain span-free
instead of inventing a location. Changed terms, missing dimensions, invalid
relations/spans or a normal PASS without the commercial report block the worker
without a publishable result. Uncertain coverage, an uncertain dimension or an
all-absent report instead preserve the candidate as a low-confidence fidelity
result. That result is fail-closed and
requires exactly one independently verified second-provider or qualified-human
review before signing; it is neither an automatic retry nor permission to
publish. The evidence request and independent verifier both receive the bound
commercial profile and policy context. The old known-good translation remains.
Do not classify legal text as commercial to bypass the legal human-review gate.

The full commercial response hash stays in the normal quality-pass receipt;
job IDs bind the profile version through queue, signed memory and publication.
As before, the host must verify an independent quality receipt before signing.
Schema validation does not prove that a model's semantic findings are true or
complete. The receipt verifier must validate evidence held by the trusted host;
the worker retains hashes, not reviewer prose. No new provider is hardwired.

Premortem: counting digits could reject native number words yet accept swapped
tariff prices, while treating uncertainty as an ordinary PASS could bypass the
release gate. The profile instead requires per-offer semantic comparison,
allows equivalent locale forms, and converts unresolved evidence into a bound
low-confidence review route. Keeping source evidence out of the native pass
prevents source-shaped copy from receiving an
artificial advantage. Version-bound job IDs prevent old policy/cache reuse.
Tests exercise the contract across all 24 locale routes, ten defect dimensions,
native digit/number-word representations, multiple offers, source blindness,
queue terminal failures and the actual worker-to-signed-publication path.
Scripted adapters test enforcement, not real native quality or DeepL superiority.

## One-transition service loop

`integrations/website_localization_service.py` composes the queue runner,
quality-evidence coordinator, signed release store, and CMS outbox into one
host-callable tick. It opens no database, socket, credential, or model by
itself. The host injects the provider and asset resolvers, evidence adapter,
receipt verifiers, signing authorities, publisher, worker identities, clock,
and durable stores.

Each tick performs at most one externally active pipeline step. A due signed
CMS delivery has first priority; otherwise the service advances at most one
completed locale through independent evidence and signed approval; otherwise
it claims and processes at most one translation job. Active leases and backoff
windows remain untouched. An existing delivery that is not yet due and an
evidence request waiting for retry do not prevent unrelated queued work from
advancing. Successful delivery removes that event from future scheduling.

The return schema `blun.website-localization-service-tick.v1` contains only the
phase, status, stable event/plan/job/delivery IDs, target locale, attempt, and
stable error code. It never contains source or target text, reviewer prose,
receipts, signatures, provider exceptions, or transport details. A blocked
signature, database, queue, evidence, or publication transition cannot fall
through to a weaker phase in the same tick.

The runner accepts the narrow `LocalizationQueue` transition contract rather
than a process-local Python class identity. This matters because the public
files are independently loadable adapters: a queue created by the CMS bridge
can now be passed to the runner without copying state or opening a second
database. The host remains trusted and the queue itself still validates every
payload, lease, hash, and transition transactionally.

Premortem: a scheduler could publish before all locales are signed, call a
translation provider after an evidence failure, starve a ready outbox behind a
large queue, leak prose in operational status, or duplicate work after a
restart. Delivery-first ordering, one active transition per tick, immediate
fail-closed return, content-free outcomes, and reuse of the existing durable
leases and idempotency keys address those failures. End-to-end tests count the
provider, evidence, and publisher calls across translation, approval, and
delivery ticks; they also cover delivery priority, provider/evidence/publisher
failure, event tampering, and an idle completed service.

## Durable service supervisor

`integrations/website_localization_supervisor.py` turns the host-configured
service tick into a long-running, restartable process without taking ownership
of databases, credentials, provider selection, signing keys, or signal
handling. The host supplies a dedicated SQLite connection, the configured tick
callable, a stable worker ID, and—when running continuously—a stop predicate.
The supervisor invokes exactly one service tick at a time and checks for a stop
only between those atomic units.

The supervisor uses one transactional, expiring lease. A second process sees a
live lease and performs no work; after a crash, another process may claim only
after the exact expiry time. Lease completion is bound to both worker ID and a
fresh random token, so an old process cannot overwrite a recovered process's
schedule or heartbeat. The service tick's own narrower queue, evidence, and
delivery leases remain the final protection for any external operation that
outlives the supervisor lease.

Successful active work receives a short configurable delay, an idle result a
longer delay, and `blocked`, `failed`, or `retry_wait` results bounded
exponential backoff. Continuous operation caps sleeps by a separate stop-poll
interval, allowing prompt graceful shutdown without interrupting a tick.
Durable status reports the next tick, live or recoverable lease state,
consecutive blocked count, last start/finish time, phase, status, and stable
error code. It never returns the lease owner or token, customer content,
provider output, exception text, receipts, signatures, or secrets.
When attached to `LocalizationHealthMonitor`, a configurable staleness window
also marks a long-overdue ready tick as `supervisor.heartbeat_stale`; this
prevents an exited process from appearing healthy merely because no lease is
currently held.

Hosts should keep the supervisor connection on durable local storage, use a
lease longer than the maximum expected tick duration, install their normal
process manager's stop signal into the predicate, and treat
`supervisor.tick.unhandled`, altered state, or an expired lease as degraded
health requiring operator attention. The library deliberately does not open a
socket, daemonize itself, modify an OS scheduler, invent a model, or publish a
partial localization.

Premortem: duplicate supervisors could call providers concurrently, a crash
could retain ownership forever, a failing adapter could create a hot loop, a
recovered old process could overwrite newer state, or an exception could copy
customer prose into operations data. Transactional token-bound expiring
leases, bounded delays, stale-completion rejection, structural tick validation,
fixed exception codes, and an overdue-heartbeat check close those paths. Tests use two SQLite connections
to prove exclusion and crash recovery, then cover backoff caps, graceful stop,
state tampering, malformed results, invalid clocks, and prose redaction.

## Provider-neutral runtime composition

`integrations/website_localization_runtime.py` is the composition root for a
host that wants to operate the complete service rather than assemble each
adapter manually. It creates one canonical queue, signed release store, CMS
bridge, durable quality-evidence store, service tick, supervisor, and health
monitor. `run_once`, `run_forever`, and `health` all address those same object
instances and durable records. This avoids Python class-identity mismatches
between independently loadable adapter files while retaining their public
structural contracts.

The host supplies five distinct `sqlite3.Connection` objects: queue, release,
CMS, evidence, and supervisor. They may point to host-chosen durable files but
must not be the same connection because the stores have independent schemas,
transactions, and migration rules. The runtime neither opens nor closes those
connections. It also never reads a configuration file, environment variable,
credential, signing key, or network endpoint.

The supervisor lease must be strictly longer than every effective translation,
quality-evidence, and CMS-delivery lease. The runtime checks this hierarchy
before any component creates or migrates a schema. Its default supervisor lease
is 360 seconds for the three 300-second operation defaults. Custom values remain
valid only when the supervisor continues to outlive the longest operation
lease. Immediately before cache or adapter resolution, each model phase,
quality-evidence acquisition, and CMS publication, the child path renews the
exact token-bound outer lease and fails before the external call if that lease
was lost. An expired outer lease cannot finish a tick even when no replacement
has claimed it. Together these checks prevent a second service instance from
taking the outer lease while the first instance still owns a legitimate inner
operation lease. Each external adapter must additionally impose a transport
deadline shorter than its operation lease; deterministic request and delivery
identifiers still cover an uncertain remote acceptance at that deadline.

The `dependencies` mapping must contain exactly the configured provider and
asset resolvers, evidence provider, quality verifier, inbound event verifier,
approval and publication authorities, CMS publisher, three worker IDs, and an
evidence revision. Optional values are limited to the documented lease, retry,
attempt, approval-expiry, human-review verifier, and independent-model-review
verifier settings accepted by the service tick. A host-supplied result cache is
rejected before schema creation; the runtime inserts only its own signed local
translation-memory adapter. Unknown and missing keys, invalid capabilities, identifiers,
retry ranges, duplicate connections, and already-active host transactions
block before any store schema is created. The mapping is copied and frozen;
runtime status and `repr` never include its objects or values.

Supervisor policy is supplied as an exact plain mapping rather than a Python
class instance, so loading the public supervisor and runtime files under
different module names cannot break configuration. The runtime constructs its
own canonical policy after validating all six positive, finite timing values.
Its health method always reuses the event, approval, and publication verifiers
that were validated during composition; a caller can add only the optional
provider health probe and check time.

Premortem: independently loaded modules could reject the same bridge, one
SQLite handle could mix incompatible state machines, a missing publisher could
be discovered only after a job is claimed, mutable configuration could swap a
signer during operation, or diagnostic formatting could reveal a secret.
Canonical construction, structural health contracts, pre-mutation validation,
five distinct connections, a strict outer-before-inner lease hierarchy, a
frozen dependency copy, and a fixed content-free
representation close those paths. An end-to-end test sends one signed event
through Finnish translation, evidence, approval, publication, supervisor, and
health using the single composed runtime; separate tests prove invalid
capabilities and connections cause no schema writes.

## Read-only health and readiness monitor

`integrations/website_localization_health.py` gives operators one
provider-neutral, content-free view across the queue, signed translation
memory, quality-evidence state, CMS events, publication outbox, and configured
model endpoints. It accepts the same host-owned event, approval, and
publication verifiers as the runtime, an optional `ProviderHealthProbe`, and
the coordinator's optional `QualityEvidenceStateStore`. A check performs no
repair, retry, lease transition, signing action, or CMS call.

The provider probe receives only `provider_id`, `model_id`, and
`model_version`—never source text, target text, glossary terms, or reviewer
findings—and must return exactly:

```json
{
  "schema": "blun.localization-provider-health.v1",
  "provider": {
    "id": "customer-llm",
    "model_id": "king",
    "model_version": "2026-08-29"
  },
  "status": "healthy"
}
```

For every check, the monitor verifies every configured SQLite schema and
database, queued payload and result hashes, evidence-to-event/plan/job/result
bindings, deterministic evidence request IDs, stored approval bytes and
signatures, authenticated CMS events, publication payload hashes and
signatures, live lease times, and approval expiry before pending publication.
Missing or malformed provider probes, signature failures, tampering, and
unreadable state make the report `blocked`. Recoverable operational state such
as an expired evidence or worker lease, failed evidence review, failed locale,
retrying delivery, or an expired current approval is `degraded`. A live
evidence lease and ordinary pending work remain healthy.

Each website version reports one lifecycle state: `processing`,
`localization_failed`, `awaiting_approval`, `ready`, `publishing`,
`publication_failed`, or `published`. The report includes only site, version,
plan and event identifiers, counts, locale names, and stable failure codes.
Source and target text, exception messages, provider responses, receipts, and
transport details are never returned. Stable queue and outbox errors remain
actionable, while free-form details stay represented only by their stored
hashes. The separate `evidence` component reports pending, leased, retrying,
succeeded, and failed counts plus stable reasons such as
`evidence.lease_expired` or `evidence.review_failed`.

Premortem: a dashboard could report healthy after stored bytes were altered,
mutate leases while merely observing them, or leak customer content through a
provider exception. The monitor rechecks canonical bytes and isolated
signatures, regression-tests that SQLite `total_changes` stays constant, and
reduces all external failures to fixed codes. Tests also cover queue and CMS
tampering, altered evidence schemas and bindings, live and expired evidence
leases, stable evidence failures, expired approvals, missing providers,
partial work, retrying acknowledgements, ready bundles, and successful
publication.
