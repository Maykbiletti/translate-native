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
- target locale and content type;
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
fields, and use NFC text. A wrong locale, malformed response, failed review,
provider exception, or changed job binding blocks without producing a queue
result.

After both LLM reviews pass, the bundled local translation guard independently
checks Unicode NFC, HTML/JSON/XML structure, placeholders, links, code,
protected tokens, untranslated segments, and major omissions. Worker results
bind source and target hashes, locales, content type, glossary and policy
versions, provider/model identity, software version, and hashes of all three
requests and responses. They retain no reviewer prose and still set
`release_required: true`; queue success therefore remains neither a signed
release nor publication permission. Legal content additionally sets
`human_review_required: true`.

Premortem: a provider could answer in the wrong locale, merge creation and
review, leak the source into the native-only judgment, return convincing but
unstructured prose, or pass a candidate with a broken placeholder. Exact
phase and locale schemas, separate inputs, ordered calls, version matching,
response hashes, and the final local integrity gate make each case fail closed.

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

The runner's optional `result_cache` is deliberately consulted before asset or
provider resolution. Production hosts should pass only
`LocalizationReleaseStore.verified_result_cache(authority)`. That adapter
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
approval lifetime, and signing-key identity. Legal content additionally needs
a separately verified human-review receipt. Raw receipts are never stored.
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

## CMS change and publication contract

`integrations/website_localization_cms.py` connects the pipeline to a CMS
without choosing a vendor or network library. The host supplies three isolated
capabilities: an inbound signature verifier, an outbound signing authority,
and a publisher implementing `publish(CMSPublicationRequest)`. The bridge does
not read keys, open sockets, choose credentials, or update live CMS state by
itself.

An inbound `blun.cms-content-change.v1` event has exactly these fields:

```json
{
  "schema": "blun.cms-content-change.v1",
  "event_id": "cms-event-184",
  "site_id": "blun-marketing",
  "website_version": "website-2026-08-29.1",
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

After every required locale has a valid signed approval, `prepare_delivery`
creates one `blun.cms-localization-publication.v1` payload for the complete
locale set. It includes the site and website version, source identity and hash,
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

Wrong or malformed acknowledgements retry with bounded exponential backoff;
explicit permanent rejections become terminal. Crashed leases are recovered,
but stale workers cannot acknowledge a later attempt. Free-form transport
details are stored only as SHA-256 hashes. Delivery is at least once, so a CMS
adapter must make the stable `delivery_id` idempotent: acceptance followed by a
crash may send the exact same signed payload again. A failed new website
version never deletes or overwrites an older successful delivery.

Premortem: an attacker could reuse an event ID with changed content, a partial
locale set could reach the CMS, an acknowledgement could name another payload,
or a worker could wake after its lease or approval expired. Canonical inbound
signatures and collision checks block changed events; the release gate creates
only complete bundles; exact signed payload hashes bind acknowledgements; and
both leases and approval expiries are rechecked immediately before delivery.
Regression tests cover replay, collision, partial readiness, tampering, exact
acknowledgements, bounded retries, opaque failures, and crash recovery.

## Read-only health and readiness monitor

`integrations/website_localization_health.py` gives operators one
provider-neutral, content-free view across the queue, signed translation
memory, CMS events, publication outbox, and configured model endpoints. It
accepts the same host-owned event, approval, and publication verifiers as the
runtime plus an optional `ProviderHealthProbe`. A check performs no repair,
retry, lease transition, signing action, or CMS call.

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

For every check, the monitor verifies all three SQLite schemas and databases,
queued payload and result hashes, stored approval bytes and signatures,
authenticated CMS events, publication payload hashes and signatures, live
lease times, and approval expiry before pending publication. Missing or
malformed provider probes, signature failures, tampering, and unreadable state
make the report `blocked`. Recoverable operational state such as expired
leases, failed locales, retrying delivery, or an expired current approval is
`degraded`. Ordinary pending work remains healthy.

Each website version reports one lifecycle state: `processing`,
`localization_failed`, `awaiting_approval`, `ready`, `publishing`,
`publication_failed`, or `published`. The report includes only site, version,
plan and event identifiers, counts, locale names, and stable failure codes.
Source and target text, exception messages, provider responses, receipts, and
transport details are never returned. Stable queue and outbox errors remain
actionable, while free-form details stay represented only by their stored
hashes.

Premortem: a dashboard could report healthy after stored bytes were altered,
mutate leases while merely observing them, or leak customer content through a
provider exception. The monitor rechecks canonical bytes and isolated
signatures, regression-tests that SQLite `total_changes` stays constant, and
reduces all external failures to fixed codes. Tests also cover queue and CMS
tampering, expired leases and approvals, missing providers, partial work,
retrying acknowledgements, ready bundles, and successful publication.
