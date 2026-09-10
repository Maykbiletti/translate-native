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

### Secure HTTP provider adapter

`integrations/website_localization_http_provider.py` is the bundled transport
for connecting a host-owned model gateway to the worker contract. It sends one
request for one locale and one phase, binds every response to the deterministic
request ID and canonical request hash, obtains credentials from a host callback,
rejects redirects, and leaves all bounded retries to the durable queue. The
adapter is vendor-neutral and can sit in front of a user's own LLM or any
provider-specific proxy without placing credentials or vendor logic in jobs.

The complete public envelope, authentication, idempotency, response, and
failure contract is documented in
[`WEBSITE_LOCALIZATION_HTTP_PROVIDER.md`](WEBSITE_LOCALIZATION_HTTP_PROVIDER.md).
The transport proves neither native quality nor superiority over an external
baseline; those decisions remain with the two independent review stages and
the blinded benchmark.

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

### Official DeepL baseline acquisition

`integrations/website_localization_deepl_baseline.py` is the optional concrete
input adapter for that provider-neutral gate. The host chooses only `free` or
`pro`; the adapter derives the corresponding documented origin and does not
accept an arbitrary URL. It obtains the API key from a callback immediately
before each request, sends it with the documented `DeepL-Auth-Key` scheme, and
disables redirects. Neither the key nor the raw provider response envelope
appears in an exception, representation, artifact, or provenance-evidence
record; the exact translated target is retained only where the benchmark
artifact contract requires it.

Before translation, the adapter queries
`GET /v3/languages?resource=translate_text` and caches a validated stable
capability snapshot for no more than one hour. It prefers an exact BCP-47
variant and otherwise uses a provider-advertised base language only when that
base is explicitly usable in the required source or target role. New stable
languages can therefore become available without a release, while removed,
beta, malformed, or unsupported entries block fail-closed. In particular, the
adapter does not manufacture a Maltese result when the current API capability
response does not advertise `mt` as a target.

One benchmark case produces one `POST /v2/translate` request. Its `text` array
contains exactly the complete bound source document, never separately scored
segments or several locales; the request selects `prefer_quality_optimized`
and preserves formatting. The adapter enforces DeepL's 128-KiB request limit,
a bounded response, strict UTF-8 JSON with no duplicate keys, exactly one
translation, source-language consistency, NFC target text, and the benchmark's
target-size limit. It classifies HTTP 429 and 5xx responses as retryable for
the campaign's existing bounded exponential backoff. Authentication, quota,
other HTTP 4xx, unsupported-language, schema, binding, and attestation failures
remain terminal. Adapter errors carry a validated content-free campaign marker,
so `run_next_benchmark_case` stores only a stable code and retry decision.

On success, `BaselineAcquisition.artifact` is the existing signed benchmark
artifact. `BaselineAcquisition.evidence` contains only endpoint, language,
model label, and exact request/response/source/target digests. Its canonical
digest becomes the artifact's `official_api` provenance; the host must retain
that evidence beside its authorized API audit record.

`BaselineAcquisitionStore` provides the durable hand-off between acquisition
and blind review. Give it a dedicated host-owned SQLite connection and resolve
each acquisition with `resolve_baseline_acquisition`. The caller supplies a
stable route ID such as an account/environment reference that contains no
credential. Store identity binds that route, the complete validated benchmark
policy, and the complete validated suite job. It therefore changes with the
source, locale, candidate configuration, suite, baseline identity or version,
quality policy, and every other benchmark-policy field.

The resolver first reads and reverifies the exact stored artifact, provenance
evidence, canonical hashes, and host attestation. A valid hit is returned
without calling the acquisition callback; a missing row calls it once and
persists the complete acquisition before review. Corrupt state is not a cache
miss: it blocks before any callback or provider request. Concurrent identical
writes converge, while a different valid output under the same identity is a
terminal conflict and never replaces the first acquisition. Rotate
`baseline_version` deliberately when a fresh current-API comparison is
required.

The database necessarily contains the baseline target because the blind
benchmark consumes it. Keep the database owner-only and apply host storage
encryption, backup, retention, and deletion policy appropriate to the source
content; never place API keys or raw provider envelopes in it. A separate
campaign database may retain only the already defined text-free case results.
Constructing the store neither opens a network connection nor invents a
fallback translation.

Long-running hosts should also pass an `operation_guard` to
`DeepLBaselineAdapter`. It runs immediately before credentials are requested
and before each Languages or Translate API call. A lost lease therefore blocks
the external operation; the campaign still owns bounded retry and terminal
failure policy.

For a provider-unsupported locale, the host may instead call
`create_lawful_fixture_acquisition`. The fixed target remains host-supplied and
must match a strict evidence record binding fixture ID and revision, supplier,
rights basis (`owned`, `licensed`, or `permission`), rights-evidence digest,
source digest, target locale, and target digest. The function neither retrieves
nor creates a translation. The host-owned authority attests the resulting
`lawful_fixture` artifact, and the host remains responsible for the truth and
retention of the underlying licence or permission.

The implementation follows DeepL's official
[translation request](https://developers.deepl.com/api-reference/translate/request-translation),
[Languages API](https://developers.deepl.com/docs/languages/using-the-languages-api),
[error handling](https://developers.deepl.com/docs/best-practices/error-handling),
and [usage limits](https://developers.deepl.com/docs/resources/usage-limits)
documentation. No API credential or real baseline output is included in this
repository.

`integrations/website_localization_benchmark_candidate.py` gives the attached
customer-model candidate the same crash-safe identity discipline as the
baseline. `resolve_candidate_acquisition` first looks up a candidate under a
stable, non-secret model-route ID plus the complete benchmark policy and
canonical suite job. A verified hit returns the exact previous worker result
without calling the model. A miss runs the ordinary three-phase localization
worker—transcreation, target-only native review, then source-aware fidelity
review—and persists the first complete result before blind comparison.

The candidate artifact contains the complete worker result and is signed by
the configured host-owned benchmark evidence authority. Every read checks the
store digest, reconstructs every job, source, locale, model, version, quality
profile, review-confidence, and target-hash binding, and reverifies that
attestation. Recomputing the database digest after changing target text is
therefore insufficient. Corrupt or differently signed content blocks before
model access. Identical writers converge; a different valid result under the
same route, policy, and job is a terminal conflict and cannot replace the first
candidate. Deliberate reevaluation requires a changed bound model version,
policy, suite, or route rather than deleting or overwriting evidence.

Pass the campaign lease guard as `operation_guard`. A guarded adapter checks it
immediately before each of the three model calls and before every authority
sign or verify call, including verification of a cached result. Model,
temporary authority, and lease failures retain stable retryability and flow
into the campaign's bounded content-free retry policy. Invalid jobs, assets,
responses, stored state, attestations, and conflicts remain fail-closed.

This dedicated database necessarily retains candidate source-derived text and
review metadata. Keep it owner-only and apply storage encryption, retention,
backup, and deletion controls appropriate to the source. It contains no model
credential or raw provider exception. The separate campaign database still
stores only attested text-free case results.

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

`integrations/website_localization_native_reference_store.py` provides the
durable hand-off from that qualified-human workflow into a benchmark campaign.
Give `NativeReferenceArtifactStore` a dedicated host-owned SQLite connection
and a stable route ID identifying the approved vault or editorial workflow,
never a credential. The record identity binds that route, the complete
benchmark policy, and the complete canonical suite job. A source, locale,
candidate configuration, policy, suite, quality profile, verifier, reference
revision, or route change therefore cannot inherit an older reference.

Every read checks the stored JSON and digest, then reverifies the artifact's
host attestation and qualified-native receipt against the exact current job.
Corrupt state blocks instead of becoming a miss. Identical independent writes
converge, while a different valid artifact under the same identity is a
terminal conflict and never replaces the first. The database necessarily
contains the complete reference target and receipt; keep it owner-only and
apply the host's encryption, backup, retention, and deletion policy. The
campaign database continues to retain only text-free case evidence.

`resolve_native_reference_artifact` returns a verified stored artifact or calls
one host-supplied external loader and persists its result before blind review.
Its optional operation guard runs before cached receipt verification, before
the external lookup, and before verification on save. Guard, loader, temporary
attestation-authority, and receipt-verifier outages become bounded,
content-free campaign dependency failures. Invalid artifacts, altered state,
binding mismatches, and conflicts remain terminal; no missing reference is
generated locally.

`integrations/website_localization_native_reference_intake.py` completes the
editorial hand-off without defining a vendor or inventing a translation. A
host creates one `blun.website-localization-native-reference-work-order.v1`
object per exact suite job. It contains the complete source, target locale,
content type, locale-quality profile, policy and suite bindings, but no target
text, reviewer credential, receipt, candidate, or baseline. Its stable identity
binds the current route, complete policy, and complete canonical job.

After a separately qualified native editor supplies a target, use
`native_reference_verification_request_for_work_order` to construct the exact
request that the configured verification authority must authorize. Submit that
request and opaque receipt as
`blun.website-localization-native-reference-submission.v1`, including the exact
work-order ID and SHA-256 digest. `accept_native_reference_submission`
regenerates the work order from current trusted state, reconstructs the receipt
payload, verifies the qualified-human receipt, obtains and verifies the host
attestation, then saves through `NativeReferenceArtifactStore`. A stale order,
changed source, locale, profile, suite, policy, route, reviewer, target, or
receipt therefore blocks before storage. Pass the campaign lease guard as
`operation_guard`; it is checked immediately before every verifier and
attestation-authority operation. Temporary guard, verifier, signer, or
attestation-verifier outages are retryable and content-free. Identical
submissions converge, while a different valid submission cannot replace the
first accepted reference.

The work order and accepted artifact contain source-derived prose and the
accepted artifact contains the native target and receipt. Transport them only
over a host-authenticated channel and retain them in owner-only storage under
the host's encryption, access, backup, retention, and deletion policy. Public
status must expose only stable IDs, hashes, codes, and counts. The intake
module performs no network call, reads no credential or environment variable,
and deliberately leaves authentication and qualified-review operations to the
host adapters.

`integrations/website_localization_native_reference_queue.py` makes that hand-off
durable for a complete campaign. It creates exactly one text-free queue row for
every policy-required `(target_locale, suite_case_key)` pair in the campaign
database. `claim_native_reference_work_order` returns one expiring,
token-bound lease and its exact target-free work order; concurrent editors
cannot claim the same live row. The lease payload contains the source and is a
private editorial artifact even though it contains no target. Renew it with
`renew_native_reference_work_order` when a qualified review legitimately needs
more time.

Submit only through `accept_native_reference_submission` on the production
runtime. The runtime checks the exact live queue lease before each receipt or
attestation operation, saves the verified artifact in the separate native-
reference store, and only then records the artifact SHA-256 digest as queue
success. A verifier or authority outage enters bounded exponential retry;
malformed, replayed, stale, or rejected evidence becomes a terminal stable
error without storing its prose. Expired leases are recovered with a new token
and stop permanently at the configured attempt limit. If a process crashes
after the artifact commit but before queue completion, the next claim
reverifies that immutable artifact and reconciles the queue without asking a
model or external reference loader to create replacement text.

`native_reference_queue_status` and `native_reference_queue_health` expose only
counts, hashes, timestamps, and stable codes. Health is rollback-only and
blocks on corrupt state, terminal failures, or an expired benchmark policy;
expired leases, due retries, and stalled progress degrade visibly. A transport
adapter must keep the lease token and work-order source private, authenticate
the editor, reject duplicate JSON keys and oversized bodies, and map the
runtime's exact content-free outcomes without weakening these checks.

`integrations/website_localization_native_reference_http.py` provides that
transport as a provider-neutral WSGI application. The host supplies one
authenticator; after checking the complete method, path, normalized headers,
and request-body SHA-256 digest, it must return a credential-bound editor ID
and exactly one qualified BCP-47 target locale. The client cannot choose or
override either value. The application requires an effective HTTPS WSGI
scheme, rejects query strings, transfer encoding, missing or false content
lengths, non-JSON media types, BOMs, duplicate keys, non-finite numbers,
invalid UTF-8, extra fields, and bodies above 4 MiB. Authentication happens
before JSON decoding, and neither credentials nor exception prose enter a
response.

The private endpoints are:

- `POST /v1/native-references/claim` with the exact claim schema, a durable
  request ID, and a lease duration. The queue filters by the authenticated
  locale. Replaying the same request while its lease is live returns the exact
  same lease and cannot reserve a second source; replay after expiry or reuse
  under another credential fails closed.
- `POST /v1/native-references/renew` with the exact private lease envelope and
  a new duration. The runtime reconstructs the canonical job from policy and
  suite state, then requires the authenticated editor, locale, work ID,
  attempt, token, expiry, and complete work order to match the live row. Lease
  renewal and its content-free response journal commit in one transaction, so
  a retry after a lost response returns the byte-equivalent lease envelope.
- `POST /v1/native-references/submit` with that lease and the complete
  transport-neutral submission envelope below. Receipt verification,
  attestation, immutable artifact storage, and queue completion remain inside
  the runtime. The request is reserved before external verification, and its
  content-free outcome is committed atomically with the queue transition. An
  exact retry returns that outcome without invoking the verifier or storage
  again; a repeated or stale request cannot store a second result.
- `GET /v1/native-references/status`, which returns only the authenticated
  locale plus the existing content-free counts, timestamps, hashes, and stable
  codes, including processing, completed, and abandoned HTTP-request counts.
  It never returns a source, target, receipt, credential, request ID, or lease
  token.

Every response sets `Cache-Control: no-store` and
`X-Content-Type-Options: nosniff`. A claim response necessarily contains the
source and lease token, so operators must also prevent proxy/access-log body
capture and apply owner-only retention to request bodies. When TLS terminates
before WSGI, only a trusted proxy may set the effective HTTPS scheme;
forwarding an untrusted client header is not sufficient. Request IDs are
idempotency keys, not evidence and not authorization.

Renewal and submission journals bind the request ID to the exact body digest,
campaign, credential-derived editor identity, locale, work item, attempt,
lease token, and original expiry. Reusing an ID with another operation, body,
credential, or locale is a conflict. Journals retain only renewal expiry or
the existing content-free queue outcome—never source text, target text,
receipts, work orders, or credentials. Concurrent submission retries receive
a retryable in-progress conflict. If the worker crashes before a queue
transition, the processing entry expires with its work lease; normal bounded
queue recovery issues a new attempt and the abandoned request ID remains
unusable.

The three write requests use these exact outer shapes; `lease` is the complete
claim response value and `submission` is the complete envelope in the next
section:

```json
{"schema":"blun.website-localization-native-reference-http-claim.v1","request_id":"<idempotency key>","lease_seconds":3600}
{"schema":"blun.website-localization-native-reference-http-renew.v1","request_id":"<idempotency key>","lease":{},"lease_seconds":3600}
{"schema":"blun.website-localization-native-reference-http-submit.v1","request_id":"<idempotency key>","lease":{},"submission":{}}
```

The authentication adapter receives
`blun.website-localization-native-reference-http-auth.v1` with the exact HTTP
method, path, normalized headers, and body digest. It returns exactly
`schema`, `editor_id`, `target_locale`, `credential_id`, and
`credential_version` under
`blun.website-localization-native-reference-editor.v1`. The WSGI application
validates this shape but does not decide whether the credential is qualified;
that trust decision belongs to the host authenticator and its separately
managed registry.

The transport-neutral submission envelope has exactly these top-level fields:

```json
{
  "schema": "blun.website-localization-native-reference-submission.v1",
  "work_order_id": "native-reference-work-order:<sha256>",
  "work_order_sha256": "<sha256 of the complete canonical work order>",
  "verification_request": {
    "schema": "blun.website-localization-native-reference-request.v1",
    "reference_revision": "<policy-bound revision>",
    "suite": "<exact suite object from the work order>",
    "source": "<exact source object from the work order>",
    "target_locale": "<exact BCP-47 locale from the work order>",
    "content_type": "<exact content type from the work order>",
    "quality_profile": "<exact locale profile from the work order>",
    "localization_policy": "<exact version bindings from the work order>",
    "qualification": {
      "method": "qualified_native_human",
      "reviewer_id": "<independent reviewer ID>",
      "reviewer_version": "<credential version>",
      "verifier_id": "<exact verifier ID from the work order>",
      "verifier_version": "<exact verifier version from the work order>"
    },
    "target_text": "<NFC native reference>",
    "target_sha256": "<sha256 of the exact UTF-8 target>"
  },
  "qualification_receipt": "<opaque verifier receipt>"
}
```

Fields shown as objects must be JSON objects, not strings; the notation above
keeps the contract compact. Implementations must transmit the complete
canonical objects returned by the two intake helpers and must reject extra,
missing, duplicate, non-finite, oversized, or altered data at their transport
boundary. The host may wrap this envelope in its own authenticated HTTP,
message-queue, or editorial-system protocol, but that wrapper is not evidence
and cannot weaken the receipt, attestation, or immutable-store checks.

Every benchmark policy must bind the exact version and SHA-256 digest of the
output-free source manifest in
`integrations/website_localization_benchmark_suite.py`. Suite v4 contains 64
cases: eight independently bound cases from eight distinct domains for each of
`headline`, `cta`, `marketing`, `ui`, `documentation`, `seo`, `legal`, and
`commercial`. Eighteen cases are connected long-form pages across commercial,
marketing, documentation, and legally sensitive content. Together they
exercise HTML, JSON, placeholders, links, native register and rhythm,
translationese, negation, modality, amounts, currencies, discount and surcharge
bases, tax, deposits, trials, billing versus commitment, renewal, cancellation,
refunds, proration, tiered prices, and offer assignment. The suite contains no
target, candidate, baseline, or supposed reference translation. Actual targets
must still come from the attached candidate and lawfully acquired baseline so
unreviewed prose cannot silently become a gold standard.

The hashed manifest also binds the exact commercial evaluation scope from the
public, brand-neutral offer profile. Every commercial case carries all ten
ordered dimensions, including dimensions absent from its source so an invented
target-only claim is still in scope. The runner validates this scope before any
external review. It exposes the dimensions only to the source-aware fidelity
pass; the first native-language pass remains source-blind. Missing, additional,
or reordered dimensions block the case instead of silently narrowing review.

The policy also requires `valid_until`, an absolute positive integer Unix
timestamp chosen by the trusted host for that exact candidate, baseline,
reviewer, reference, suite, and decision configuration. It is included in the
policy hash, every signed case result, and the signed final report. Extending
the date therefore creates a new campaign and cannot relabel old case evidence
as current. The contract enforces expiry; it does not prove that a host-chosen
date is appropriate. Hosts must derive it from their lawful baseline update
process and deliberately shorten it when a provider, model, glossary, quality
profile, or evaluation policy changes.

### Durable benchmark campaigns

`integrations/website_localization_benchmark_campaign.py` turns the bound suite
and benchmark policy into a durable execution matrix. With the current
English-source suite and complete EU target scope, one campaign contains
exactly 1,472 work items: 64 source cases multiplied by 23 target locales. Work
IDs bind the complete policy hash, suite hash, locale, and case key. Creating
the same campaign again is idempotent; changing any candidate, reviewer,
reference, baseline, threshold, locale, or suite field creates a different
campaign identity instead of inheriting old evidence.

The host supplies a dedicated trusted `sqlite3.Connection`, a
`BenchmarkInputResolver`, the blind reviewer, native-reference verifier,
evidence authority, and blinding key. The resolver receives one canonical
localization job and returns `BenchmarkCaseInputs`: the already validated
candidate result, an attested baseline from an allowed acquisition route,
locale-bound assets, and an attested qualified-native reference. The campaign
store never fetches an API, chooses a provider, reads credentials, or invents a
missing artifact.

Production composition should use
`integrations/website_localization_benchmark_runtime.py`. Its
`WebsiteLocalizationBenchmarkRuntime` preflights the complete policy, routes,
adapters, blinding key, worker identity, and five distinct idle SQLite
connections before creating any schema. Those connections isolate campaign
status, candidate text, baseline text and evidence, and qualified-native
reference text and receipts, plus signed anonymous-review evidence, so one
store cannot silently share transaction or schema state with another.
Construction creates the exact idempotent campaign;
`run_once` processes at most one item, while `status`, `health`, `summarize`,
and `load_report` retain the campaign's existing text-free and
all-locales-complete contracts.

The runtime owns the only `BenchmarkCaseInputs` construction. It resolves
locale assets, then uses the durable candidate, baseline, and native-reference
stores under their exact route, policy, and canonical suite-job identities.
The configured candidate adapter is resolved lazily only when no verified
candidate is stored. The baseline callback receives the
job, bound policy, evidence authority, and current operation guard; an official
API adapter must pass that guard into its transport boundary. The native
reference callback receives only the canonical job and must return externally
qualified evidence—it is never asked to generate text. Corrupt or conflicting
state blocks instead of falling through to another external call.

For this trusted resolver, the campaign passes a no-argument token-bound guard
that renews the current lease. It runs before and after host resolvers, before
every candidate-model operation, throughout evidence verification, before the
baseline acquisition boundary, and before the external reference lookup. A
restart after a reviewer outage therefore reloads the same three verified
artifacts without invoking the model, baseline API or reference vault again.
Legacy callable resolvers remain compatible, but they receive only the older
single guard before dependency resolution and should not be used as the
production composition root.

Adapters may load these zero-dependency modules independently, as happens when
a host composes the campaign, DeepL baseline adapter, acquisition store, and
its own resolver without installing a Python package. Public frozen
`BenchmarkPolicy`, `BenchmarkSignature`, `BenchmarkCaseInputs`, and
`BaselineAcquisition` values from another module instance are normalized into
the receiving module only when the dataclass name, frozen status, field order,
and complete field set match exactly. Every ordinary policy, suite, job,
signature, evidence, authority, and artifact check then runs unchanged.
Mappings, mutable objects, extra or missing fields, and similarly named
lookalikes are not compatibility values and block before persistence or blind
review. This structural boundary prevents Python class identity from becoming
an accidental vendor lock while retaining fail-closed validation.

`integrations/website_localization_benchmark_review_store.py` closes the
remaining restart boundary around the two ordered blind reviews. Immediately
after a response passes the exact phase, locale, blind-ID, preference and
defect-schema checks, the runtime binds it to the canonical request hash,
benchmark policy, configured reviewer route and reviewer identity, then signs
and verifies that artifact before continuing. The source-blind
`target_native` response is therefore durable before `source_fidelity` begins.
If the second review or final campaign commit fails, a retry reverifies and
reuses the first response; after both are stored, neither review is called
again. The deterministic `review_id` remains the external adapter's
idempotency key for the unavoidable crash window after the reviewer accepts a
request but before the local transaction commits.

Stored review state is immutable. A repeated review ID with another request
hash, invalid JSON or digest, a wrong reviewer binding, failed attestation, or
a second valid but different response blocks without calling the reviewer
again. Changed routes and policies cannot reuse old evidence. Verifier outages
remain retryable, while corruption and conflicts are terminal. Operational
campaign status and final case results still expose only response hashes,
preferences, defect counts and finding hashes—not reviewer reasons, excerpts,
source text or either target.

The review store also exposes a strictly read-only, content-free health view
for one exact reviewer route and benchmark policy. It rechecks canonical rows,
digests, phases, locale and blind-variant bindings, and every host attestation;
historical policies are counted separately from the active scope. The caller
may provide the exact request and response hashes required by completed cases.
Missing, mismatched, altered, or unverifiable evidence blocks with stable
reason codes. The health payload contains only counts and never invokes the
reviewer or returns source, target, explanation, excerpt, or finding text.

### Provider-neutral benchmark reviewer HTTPS adapter

`integrations/website_localization_benchmark_reviewer_http.py` implements the
runtime's `BenchmarkReviewer` contract for an independently hosted human or
model review gateway. Configure one fixed HTTPS endpoint and a host-owned
authentication-header callback, then pass the adapter as `reviewer` in the
existing `benchmark_execution` mapping. The adapter performs exactly one
request per invocation; the durable campaign and review store remain the only
owners of retry limits, backoff, leases, and reuse.

Each `POST` body uses
`blun.website-localization-benchmark-review-http-request.v1` and contains the
exact anonymous `BenchmarkReviewRequest`, its deterministic `review_id`, and
the SHA-256 digest of its canonical UTF-8 JSON. The same values are bound in
`Idempotency-Key`, `X-Benchmark-Review-Id`,
`X-Benchmark-Review-Phase`, and
`X-Benchmark-Review-Request-Sha256`. Authentication headers are obtained for
that attempt only and cannot replace protocol, routing, framing, or binding
headers. Credentials never enter the body, error state, or durable benchmark
evidence.

The source-blind request is accepted only with the exact `target_native`
instruction and input field set; `source`, `glossary`, and `protected_terms`
are forbidden. The later `source_fidelity` request has a different exact field
set and instruction and carries the source. Both retain only anonymous `A` and
`B` variants. Candidate provider, baseline identity, acquisition provenance,
and unblinding data are absent from the transport contract.

The service must return
`blun.website-localization-benchmark-review-http-response.v1` with the same
`review_id` and request digest plus one exact benchmark-review object. The
adapter rejects wrong phase, locale or blind ID, unknown fields, malformed
defects, and a preferred variant that still has a blocking or major defect.
Responses are strict UTF-8 JSON with duplicate keys, BOMs, non-finite numbers,
wrong media types, inconsistent lengths, redirects, and oversized bodies
rejected. Only `408`, `425`, `429`, network failures, and `5xx` responses are
retryable; orchestration receives stable content-free error codes.

Plain HTTP is available only through an explicit loopback-only development
option. Production TLS termination, authentication, credential rotation,
access control, request logging policy, and reviewer independence remain host
responsibilities. This adapter makes the blind review runnable; it does not
itself establish linguistic quality or superiority over a baseline.

`run_next_benchmark_case` claims and processes at most one exact
case/locale pair. Random token-bound leases are renewed before dependency
resolution and before each of the two reviewer calls. An abandoned lease can
be recovered after expiry; stale workers cannot finish it. Retryable adapter
or attestation failures use bounded exponential backoff and a configured
attempt ceiling. Terminal binding, parser, suite, policy, and evidence failures
remain failed while unrelated work continues.

Operational status contains only deterministic work identity, counts, stable
error codes, timestamps, and hashes. The database retains only the attested
text-free case result after success—not source, candidate, baseline, reference,
credentials, or reviewer prose. `summarize` remains fail-closed until every
expected item succeeded. It then signs and verifies the exact result set outside
the database transaction, rechecks that set atomically, and stores the first
canonical report in the campaign database. The report is bound to the campaign
policy and ordered result hashes. Later calls return that same verified report
byte-for-byte without signing again. A failed, omitted, duplicated, exchanged,
or policy-stale work item therefore cannot disappear behind a partial aggregate,
and a crash cannot silently replace the report used for a claim. Existing v1
and v2 campaign databases migrate transactionally to the v3 report schema.
Case-result schema v7 and report schema v11 bind the same `valid_until` value.

After finalization, `BenchmarkCampaignStore.load_report` is the read-only
consumer boundary. It opens a consistent snapshot, requires the exact complete
result matrix and a succeeded report-finalization state, reloads the single
stored report, then rolls the transaction back before reverifying its policy,
ordered-result digest, canonical JSON, content hash, timestamp, and signature.
It never calls the signing capability and never repairs, replaces, or creates
state. Missing, incomplete, stale, future-dated, state-inconsistent, or altered
evidence therefore returns a stable failure instead of a report.

### Authenticated benchmark report HTTP reader

`integrations/website_localization_benchmark_http.py` exposes that verified
read-only boundary to an operator dashboard or evidence consumer without
granting database access. It provides exactly two HTTPS-only WSGI routes:

- `GET /v1/benchmarks/status` returns the configured campaign identity,
  policy and suite hashes, validity deadline, work and error counts, plus the
  content-free report-finalization state.
- `GET /v1/benchmarks/report` first requires the exact campaign to be complete
  and finalized, then invokes only `load_benchmark_report`. Its response binds
  the authenticated campaign ID to the canonical signed report and a SHA-256
  digest of those exact report bytes.

The host authenticator receives
`blun.website-localization-benchmark-http-auth.v1` with the exact method, path,
sorted request headers, and the empty-body digest. It must return
`blun.website-localization-benchmark-reader.v1` with `reader_id`,
`campaign_id`, `credential_id`, and `credential_version`. The application
requires the principal's campaign to equal the runtime's verified campaign
status before report loading. Authentication failure, credential rotation,
cross-campaign access, request bodies, query parameters, plaintext transport,
incomplete or expired campaigns, malformed runtime output, and report
verification failures all return only a stable code and retry flag. Responses
use `Cache-Control: no-store`; neither route starts benchmark work, signs a
report, repairs state, or returns case prose, source text, target text,
credentials, or adapter exceptions.

`BenchmarkCampaignStore.health` verifies the complete campaign binding, every
row invariant, successful result hash, and case attestation in a consistent
read-only snapshot. It reports only status counts, stable reason codes, the
latest progress timestamp, and whether the persisted final report verifies.
Health never signs or stores a report. A fully completed campaign without its
first report is degraded with `benchmark.campaign.report_missing` until an
explicit `summarize` call creates it; altered or unverifiable report bytes block
with `benchmark.campaign.report_invalid` and are never regenerated in place.
The production benchmark runtime performs that explicit finalization directly
after the last successful case. If the process stopped between committing that
case and creating the report, the next otherwise idle benchmark tick detects
the exact complete matrix and closes the gap without resolving another case.
Both paths guard the signing boundary before and after report construction, so
loss of the outer supervisor lease prevents persistence and a later tick can
retry safely. A stored report suppresses all automatic re-signing.
Expired leases and overdue actionable work degrade health; any terminal work
failure, altered row, invalid attestation, or invalid final report blocks it.
Live backoff and recent incomplete work remain healthy and never imply that the
candidate won.

At the first clock value after `valid_until`, the campaign blocks new claims,
rechecks the boundary before every dependency or reviewer operation, refuses
case completion and report signing, and rejects stored-report loads with
`benchmark.campaign.validity_expired`. An expiry discovered during a live case
or report attempt is recorded as terminal, content-free state; no remaining
external adapter is called. Health is blocked and `report_ready` is false even
when every historical score and signature remains otherwise valid.

Early locale lanes may be run and reported independently, but passing them no
longer authorizes an EU-wide superiority statement. The attested report exposes
`configured_lanes_status` separately from `superiority_claim_allowed` and
includes an exact `claim_scope` with required, evaluated, missing, unexpected,
and source-language locales plus the required and evaluated content types. A
public claim is allowed only when the configured locale set exactly covers
every EU target eligible for the bound source suite, all eight content types
are configured, and every locale report passes on its own. One missing locale,
missing content-type lane, or blocked result therefore blocks the overall
report; no aggregate can conceal it.

Each locale report also exposes separate statistics for `target_native` and
`source_fidelity`: candidate wins, baseline wins, ties, decisive rate, candidate
win rate, and the one-sided sign-test probability. Both axes must independently
meet the policy's predeclared sample, decisiveness, win-rate, and significance
thresholds. Those thresholds and fixed block reasons are included in the
attested report. Joint case winners remain an additional conservative metric,
but discarded cross-axis disagreements can no longer make a weak axis appear
statistically convincing.

Suite v4 predeclares all eight content types as required statistical lanes with
a minimum of eight cases per type and locale. For every lane, the report repeats
the joint and independent `target_native` and `source_fidelity` statistics. A
weak headline, CTA, marketing, UI, documentation, SEO, legal, or price/offer
lane blocks its locale even when wins from the other content types make the
all-content aggregate appear significant. A policy may deliberately evaluate a
smaller diagnostic subset, but its signed claim scope records every omitted
content type and cannot authorize a public superiority claim.

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
accepts a quality receipt for the complete release context. The module
never reads a signing key. Instead, a trusted `ApprovalAuthority` signs and
immediately verifies the canonical approval bytes outside the worker's
authority. Production hosts should implement that interface with an isolated
service or hardware-backed signer; the repository tests use HMAC only as a
deterministic test double.

The receipt-verifier contract receives exactly `binding` and `receipt`.
`binding` uses `blun.localization-quality-receipt-binding.v2` and contains the
review purpose, job and canonical result hashes, full source and target text
plus hashes and locales, content type, glossary and policy versions, primary
and optional review-provider identities, software version, two-pass
confidence, locale quality profile, optional commercial profile, its exact
content-free targeted-review summary, and the human/independent-review
requirements. The verifier must cryptographically
bind every field. It must reject a receipt issued for another result, policy,
model, profile, software version, locale, or review purpose. In particular, a
quality receipt cannot satisfy a qualified-human or independent-model review.

For deployments that keep verification behind a network trust boundary,
`integrations/website_localization_receipt_verifier_http.py` provides one
fixed-endpoint provider-neutral HTTPS attempt. It validates the complete
binding before authentication, derives deterministic request identity from
the binding and receipt hashes, disables redirects, strictly checks the bound
boolean response, and contains no credential or retry loop. Declared temporary
network and service failures retain retryability through the durable evidence
queue; an explicit negative verdict or invalid binding remains terminal. The
public protocol is documented in
[`WEBSITE_LOCALIZATION_RECEIPT_VERIFIER_HTTP.md`](WEBSITE_LOCALIZATION_RECEIPT_VERIFIER_HTTP.md).

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

Premortem: a receipt or signature might be replayed after policy drift, a database edit
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

For deployments that need a concrete network boundary,
`integrations/website_localization_evidence_http.py` implements that interface
as one request-bound HTTPS attempt. It validates the exact v4 evidence request,
canonicalizes native Unicode without ASCII folding, binds the inner digest and
deterministic evidence ID in both headers and body, disables redirects, and
strictly validates the response envelope before the coordinator verifies its
receipts. Authentication remains a host callback and the adapter contains no
provider-specific model, endpoint, credential, brand, product, or price. The
public protocol and retry classification are documented in
[`WEBSITE_LOCALIZATION_EVIDENCE_HTTP.md`](WEBSITE_LOCALIZATION_EVIDENCE_HTTP.md).
The adapter never retries internally; the durable evidence state below remains
the single retry authority.

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
result and policy bindings reject stale evidence; the HTTP envelope makes the
external idempotency and digest contract explicit; signed release readiness and
the all-locale CMS transaction block partial publication. Tests cover
exclusive claims, bounded retries, one-attempt HTTP failures, one-locale
progression, replay, crash recovery, expiry, legal review, authentication and
endpoint safety, Unicode transport, parsing, tampering, wrong bindings, and
failed receipt verification.
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

Before publication starts, the CMS may withdraw that exact event with a signed
`blun.cms-content-cancellation.v1` message. Its immutable binding contains a
unique cancellation ID plus the event, site, website version, source ID, and
source sequence. The signer key must match the credential that created the
event. Exact replay is idempotent; altered bindings, another accepted tenant
key, corrupt stored bytes, and cancellation-ID reuse fail closed. An accepted
cancellation removes the event from service scheduling, blocks release and
delivery preparation, and closes a pending or retrying outbox entry without
calling the model, reviewer, or publisher. Health and tenant lifecycle reads
reverify the cancellation and expose only the stable `cancelled` state.

Cancellation never rewrites an acknowledged publication. It also refuses a
currently leased publication because the external CMS may already have
accepted the request. The caller must observe the lease outcome before retrying.
Deleting content already published uses an independently signed CMS tombstone;
cancellation remains deliberately limited to unpublished work. The tombstone
is accepted only for an exactly acknowledged publication and is bound to its
delivery ID, payload hash, plan, source generation, website version, and full
sorted locale set. A separate durable, signed outbox retries delivery after a
crash and accepts only an exact signed `deleted` acknowledgement. It contains
no source or target prose and preserves the original publication as immutable
audit history.

Cancellation remains available during the durable intake crash gap, after the
signed event and source sequence are stored but before queue insertion has
finished. The bridge checks the ledger both before and after queue insertion.
Therefore an exact replay cannot revive withdrawn work, and a cancellation that
races insertion prevents the event from becoming production-eligible. Tenant
status, lifecycle, and health expose this intentionally queue-free state as
`cancelled` instead of misreporting it as an intake outage; no provider health
probe is made for that event.

The CMS signs the canonical UTF-8 JSON bytes outside the envelope. The bridge
verifies the signature before its first write, derives the deterministic plan,
and persists the event before enqueuing it. If the process stops between those
two transactions, the service supervisor reloads and verifies the stored event,
then resumes exactly one intake per tick with the same attempt ceiling as the
public API. An exact CMS replay remains safe but is no longer required for
progress. Deterministic plan and job identities make concurrent replay and
automatic recovery idempotent, while the cancellation ledger is checked again
before and after queue insertion and therefore cannot be revived.
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

The same adapter transports a tombstone without changing its security model.
It uses `blun.cms-localization-tombstone-http.v1`, nests the exact signed
object under `tombstone`, and accepts only a signed
`blun.cms-localization-tombstone-http-ack.v1` envelope whose acknowledgement
is exactly bound to the delivery ID and payload hash and has status `deleted`.
The locale list is sorted and unique, while source and target prose are absent.

The adapter also exposes an optional, content-free `check` operation for
operator health. Each call creates a fresh probe ID and sends only that ID plus
the SHA-256 digest of the advertised publication HTTP contract. The same
authentication, HTTPS-only endpoint, redirect prohibition, transport bounds,
and strict JSON parser apply. The CMS must return status `healthy` in a signed
`blun.cms-localization-publication-health-ack.v1` acknowledgement bound to the
exact probe ID and contract digest. Replayed challenges, a receiver implementing
a different contract, or an invalid signature therefore cannot make readiness
green. No source text, target text, locale, site, publication, or tombstone is
included in this request.

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

### Authenticated CMS webhook API

`integrations/website_localization_api.py` exposes the current signed CMS
change contract and content-free per-locale progress through two strict
HTTPS-only WSGI routes. The composed runtime publishes the same callable as
`runtime.cms_api`; no second bridge, queue, or database is constructed.

`POST /v2/localization/changes` accepts only a complete signed
`blun.cms-content-change.v2` event. Successful intake durably enqueues one exact
job per locale before returning. Exact replay is idempotent; changed event IDs,
reused source sequences, new schema-v1 events, and delayed superseded changes
cannot become current work. `POST /v2/localization/status` accepts a separate
short-lived signed request and returns only identifiers, counts, lease/retry
state, stable errors, and hashes. The signed site and original event credential
must match, preventing cross-site status access even when a verifier recognizes
multiple credentials.

Both routes reject plaintext transport, query strings, transfer encoding,
ambiguous or oversized JSON, and invalid framing. Status revalidates the stored
event signature, current source generation, exact plan/job/locale identities,
and every successful result before returning a complete response. Superseded,
missing, altered, or wrong-scope state blocks fail-closed without exposing
source text, target text, provider prose, signatures, or credentials. The full
public request, response, deployment, and failure contract is documented in
[CMS localization webhook API v2](WEBSITE_LOCALIZATION_API.md).

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
The content-free result summary uses
`translate-native.commercial-review-summary.v1`; the authenticated capability
response publishes its exact separately hashed machine contract, including the
ordered allowed dimensions and the invariant between status and unresolved
dimensions. Quality-evidence request v5 and receipt-binding v2 carry that exact
summary, so adapters can reject unknown, reordered or contradictory review
scope without reconstructing it from prose.
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
translation provider after an evidence failure, leave a signed intake stranded
before queue insertion, starve a ready outbox behind a large queue, leak prose
in operational status, or duplicate work after a restart. Tombstone and
delivery priority, one verified intake recovery per tick, immediate fail-closed
return, content-free outcomes, and reuse of the existing durable leases and
idempotency keys address those failures. End-to-end tests count the provider,
evidence, and publisher calls across recovery, translation, approval, and
delivery ticks; they also cover the API retry ceiling, cancellation exclusion,
stored-event tampering, adapter failures, and an idle completed service.

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
CMS, evidence, and supervisor. An optional monitored benchmark campaign adds a
sixth connection plus its exact policy, campaign ID, evidence verifier, and
staleness threshold. Monitor-only configuration remains supported and performs
no benchmark work. Both monitor-only and executing configurations may call
`benchmark_campaign_status` and `load_benchmark_report`; they delegate only to
the store's content-free status and read-only verified report paths. A runtime
without benchmark configuration returns `runtime.benchmark.unavailable`, and
unexpected adapter failures are reduced to the content-free
`runtime.benchmark.status.invalid` or `runtime.benchmark.report.invalid`
boundary code.

To execute the same campaign, supply the exact `benchmark_execution` mapping.
It adds separate `candidate_connection`, `baseline_connection`,
`native_reference_connection`, and `review_connection` stores, making ten
distinct connections in total. The remaining required fields are
`candidate_route_id`, `baseline_route_id`, `native_reference_route_id`,
`reviewer_route_id`, `assets_resolver`,
`candidate_provider_resolver`, `baseline_acquirer`,
`native_reference_loader`, `reviewer`, `native_reference_verifier`,
`blinding_key`, `worker_id`, `max_attempts`, `lease_seconds`,
`retry_base_seconds`, and `retry_max_seconds`. Extra, missing, malformed, reused,
or transaction-active values block before schema construction. The benchmark
evidence authority must both sign and verify, and the supervisor lease must
strictly exceed the configured benchmark lease.
An executing composition also rejects an already expired policy before any of
the ten stores creates or migrates a schema. Monitor-only composition may load
an expired campaign so health can expose its blocked state, but it cannot load
that campaign's report as current evidence.

The composition root constructs the canonical
`WebsiteLocalizationBenchmarkRuntime`; callers cannot replace its durable input
assembly with an unrestricted callback. All connections may point to
host-chosen durable files but must be distinct because the stores have
independent schemas, transactions, and migration rules. The runtime neither
opens nor closes them. It also never reads a configuration file, environment
variable, credential, signing key, or network endpoint.

The same composition root binds its approval and publication authorities into
the tenant-facing `runtime.cms_api`. It accepts signed change events, exact
signed cancellations of unpublished work, and exact signed tombstones for
acknowledged publications. Before submitting content, a CMS can use a
separately signed read to discover the exact current 24-locale registry,
content types, quality phases, schema versions, and locale-profile hashes as
one canonical capability object. In addition to durable change intake and
per-locale queue progress, the API exposes a purpose-bound lifecycle read that
revalidates release readiness and the signed CMS outbox. A tenant can therefore
distinguish processing, missing approvals, readiness, publication retry,
blocked publication, terminal failure, and acknowledged publication without
receiving source text, target text, receipts, signatures, or service-wide site
data. Neither read performs a state transition; the complete public contract is
in [`WEBSITE_LOCALIZATION_API.md`](WEBSITE_LOCALIZATION_API.md).

Service-wide HTTP health is disabled unless the host supplies an explicit
`health_http_authenticator`. With that capability, the same composition root
exposes `runtime.health_http`; optional `health_provider_probe` and
`health_publisher_probe` capabilities are bound to that reader and cannot be
configured on their own. The publisher probe must be the same object as the
runtime's delivery publisher, so a second endpoint cannot mask failure of the
real callback. All values are validated
before any store creates or migrates a schema. The operator endpoint is distinct
from signed tenant CMS progress because its content-free report can contain
identifiers for every configured site. Its complete authentication, response,
and failure contract is documented in
[`WEBSITE_LOCALIZATION_HEALTH_HTTP.md`](WEBSITE_LOCALIZATION_HEALTH_HTTP.md).

When configured, the existing content-free health report gains a
`benchmark_campaign` component. Full `benchmark_execution` configuration also
adds `benchmark_reviews`, bound to the same durable review store and reviewer
route used by the executor. For every succeeded campaign case, the monitor
requires exact stored request and response evidence for both ordered passes
and reverifies each artifact. Missing, mismatched, tampered, or unverifiable
review evidence blocks the whole health report even when the campaign result
itself remains valid. Output is limited to stable reasons and aggregate counts.
Its overall status becomes degraded for an expired lease or a stalled
actionable campaign and blocked for failed or unverifiable work. The
localization service supervisor does not execute benchmark cases for
monitor-only configurations. With `benchmark_execution`,
each supervised tick still runs the customer publication pipeline first. A
delivery, release, evidence, or translation transition returns immediately and
the benchmark executor is not called. Only an exact `idle` customer result may
advance at most one benchmark case. The same token-bound outer lease guard is
passed through the campaign and its durable candidate, baseline, reference,
and review operations; losing that lease blocks before the next external
boundary. A campaign with no currently actionable case preserves the ordinary
idle result. Benchmark success, bounded retry, terminal failure, and stable
error code use the existing content-free supervisor tick schema.

Premortem: a mirrored database could yield inconsistent status, a dead worker
could leave a lease that looks active, terminal cases could hide behind overall
progress, status could leak reviewer prose, an incomplete campaign could be
mistaken for a passed comparison, stored reviews could disappear behind a
still-valid case result, or background evaluation could delay a real
publication. Ten-store validation, customer-first scheduling, strict lease
hierarchy, snapshot verification, lease and staleness reasons, exact
cross-store review matching, per-row attestation checks, code-and-count-only
output, and a separate `report_ready` flag close those paths.

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
publication verifiers as the runtime, optional `ProviderHealthProbe` and
`PublisherHealthProbe` capabilities, plus the coordinator's optional
`QualityEvidenceStateStore`. A check performs no repair, retry, lease
transition, signing action, or content publication. When the publisher probe
is configured, it performs exactly one content-free CMS callback challenge.

`integrations/website_localization_health_http.py` makes that exact report
available to a separately authenticated service operator. It authenticates
before invoking the monitor, requires HTTPS and an empty query-free request,
and validates the complete returned report again before serialization. A valid
blocked assessment uses HTTP `503`; malformed monitor output or private
exceptions are reduced to stable content-free errors. No endpoint exists when
the runtime lacks the explicit operator authenticator.

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
signatures, tombstone request and delivery bindings, live lease times, and
approval expiry before pending publication.
Missing or malformed provider probes, signature failures, tampering, and
unreadable state make the report `blocked`. A configured publisher probe also
adds the `cms_publisher` component; an unavailable, malformed, unsigned, or
contract-mismatched callback blocks it without exposing transport details.
Recoverable operational state such
as an expired evidence or worker lease, failed evidence review, failed locale,
retrying delivery, or an expired current approval is `degraded`. A live
evidence lease and ordinary pending work remain healthy.

Each website version reports one lifecycle state: `cancelled`, `processing`,
`localization_failed`, `awaiting_approval`, `ready`, `publishing`,
`publication_failed`, `published`, `deleting`, `deletion_failed`, or `deleted`.
The report includes only site, version,
plan and event identifiers, counts, locale names, and stable failure codes.
Source and target text, exception messages, provider responses, receipts, and
transport details are never returned. Stable queue and outbox errors remain
actionable, while free-form details stay represented only by their stored
hashes. The separate `evidence` component reports pending, leased, retrying,
succeeded, and failed counts plus stable reasons such as
`evidence.lease_expired` or `evidence.review_failed`.

For a fully executing benchmark, the separate `benchmark_reviews` component
is equally observational. It scopes review rows to the active policy and route,
reverifies their immutable attestations, and compares them with the two pass
hash pairs referenced by every succeeded case. Historical rows remain visible
only as a count and cannot satisfy current requirements. Neither this check nor
its error path calls an adapter or returns reviewer prose or content hashes.

Premortem: a dashboard could report healthy after stored bytes were altered,
mutate leases while merely observing them, or leak customer content through a
provider exception. The monitor rechecks canonical bytes and isolated
signatures, regression-tests that SQLite `total_changes` stays constant, and
reduces all external failures to fixed codes. Tests also cover queue and CMS
tampering, altered evidence schemas and bindings, live and expired evidence
leases, stable evidence failures, expired approvals, missing providers,
partial work, retrying acknowledgements, ready bundles, and successful
publication.
