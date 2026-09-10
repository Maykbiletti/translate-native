# CMS localization webhook API v2

`WebsiteLocalizationAPI` is the provider-neutral WSGI ingress exposed by the
composed website-localization runtime. It accepts a signed current CMS change,
cancellation, or published-content tombstone, durably enqueues one job per
required locale, and returns
content-free capabilities, queue progress, or end-to-end lifecycle status. It
never calls a model, approves a translation, prepares a publication, or returns
source or target prose.

## Host setup

Use `runtime.cms_api`, or construct `WebsiteLocalizationAPI` with the exact CMS
bridge, event verifier, clock, and queue attempt limit. Mount the callable behind
a production WSGI server and TLS terminator. HTTPS is required by default. Only
a trusted proxy may derive `wsgi.url_scheme`; never trust an arbitrary forwarded
header. The host owns keys, credential-to-site provisioning, network access,
rate limits, timeouts, database backup, and request-log redaction.

This tenant-facing API deliberately does not expose service-wide health. An
operator can enable the separately authenticated `runtime.health_http` reader
described in
[`WEBSITE_LOCALIZATION_HEALTH_HTTP.md`](WEBSITE_LOCALIZATION_HEALTH_HTTP.md).

`require_https=False` is only for an authenticated loopback or test transport.
The application rejects query strings, transfer encoding, ambiguous JSON,
invalid UTF-8, a UTF-8 BOM, non-finite numbers, unknown fields, missing or
truncated lengths, and bodies over 4,000,000 bytes. The front server remains
responsible for unambiguous HTTP framing and request-smuggling protection.

## Create or resume localization work

```http
POST /v2/localization/changes HTTP/1.1
Content-Type: application/json; charset=utf-8
Content-Length: <exact UTF-8 byte count>
X-Localization-Signature-Algorithm: <configured algorithm>
X-Localization-Key-Id: <site credential identifier>
X-Localization-Signature: <signature>
```

The body is one complete `blun.cms-content-change.v2` event. Its required
`source_sequence` is a positive, monotonically increasing generation for the
exact `(site_id, localization.source_id)` pair. The nested `localization`
mapping is the planner contract documented in `WEBSITE_LOCALIZATION.md` and
binds the source revision and text, source locale, content type, glossary and
policy versions, provider/model/software identity, and optional target locales.
Omitting `target_locales` selects every supported EU official-language locale
except an EU source language.

Sign the UTF-8 bytes of canonical JSON: sorted object keys, no insignificant
spaces, native Unicode unescaped, NFC text, and no `NaN` or infinity. Whitespace
in the transmitted JSON may differ because the service parses strictly and
reconstructs the canonical payload before verification.

New current work returns `202 Accepted`. An exact replay returns `200 OK` and
`inserted_jobs: 0`, allowing safe recovery after an uncertain response. Reusing
an event ID or source sequence for different content returns `409 Conflict`.
A delayed event below an already accepted source generation is recorded as
`superseded` and returns `200 OK`; it can never become publication work.

If the process stops after persisting the signed event but before the separate
queue transaction, the service supervisor revalidates the stored signature and
automatically resumes exactly one such event per tick. It uses the same queue
attempt limit configured for this API. The deterministic plan and job IDs make
that recovery idempotent; an accepted cancellation remains terminal and is
never resumed. A missing or invalid signature, changed stored bytes, or queue
failure blocks before a model call and stays visible through health as
`cms.event.awaiting_queue_resume` until a valid recovery succeeds.

```json
{"event_id":"cms-event-184","inserted_jobs":23,"job_count":23,"plan_id":"blun-l10n-plan-…","schema":"blun.website-localization-api.v2","status":"enqueued"}
```

The response contains identifiers and counts only. Acceptance is not quality
approval or publication readiness.

## Cancel unpublished localization work

A CMS can withdraw one exact accepted event without submitting replacement
text:

```http
POST /v2/localization/cancellations HTTP/1.1
Content-Type: application/json; charset=utf-8
Content-Length: <exact UTF-8 byte count>
X-Localization-Signature-Algorithm: <configured algorithm>
X-Localization-Key-Id: <same site credential that created the event>
X-Localization-Signature: <signature>
```

```json
{"cancellation_id":"cms-cancellation-184","event_id":"cms-event-184","schema":"blun.cms-content-cancellation.v1","site_id":"public-site","source_id":"homepage.hero","source_sequence":184,"website_version":"website-2026-08-29.1"}
```

Sign the complete canonical cancellation object. The service requires every
event, site, source, signed source sequence, website-version, and credential
binding to match the stored event. The first accepted cancellation returns
`202 Accepted`; exact replay returns `200 OK`. Reusing either the cancellation
ID or event binding for different bytes returns `409 Conflict`.

Acceptance permanently removes that event from production scheduling, blocks
new approvals and publication preparation, and terminally closes any pending or
retrying outbox entry with `event_cancelled`. The cancellation ledger is
immutable and reverified on reads and health checks. It does not delete shared
queue artifacts or translation memory because another current plan may validly
reference the same deterministic job.

The same operation also closes the crash gap after the signed event has been
stored but before its locale plan reaches the queue. Such an `accepted` event
can be cancelled without first replaying it. Status and lifecycle then report
every required locale as `cancelled` even though no queue row exists. An exact
change-event replay returns `cancelled` and cannot recreate work. If the
cancellation arrives while the atomic queue insertion is running, the bridge
rechecks the immutable ledger before promoting the event to `enqueued`; any
already-created shared queue artifacts remain ineligible through that event.

A confirmed publication cannot be cancelled through this endpoint. A currently
leased delivery also returns `409 Conflict`: after an external request starts,
the service cannot truthfully retract bytes that the CMS may already have
accepted. The caller must wait for the lease outcome; an accepted publication
remains immutable, while a failed or retryable delivery can then be cancelled.
Removing content that was already published uses the separately signed
tombstone operation below.

## Delete an acknowledged publication

```http
POST /v2/localization/tombstones HTTP/1.1
Content-Type: application/json; charset=utf-8
Content-Length: <exact UTF-8 byte count>
X-Localization-Signature-Algorithm: <configured algorithm>
X-Localization-Key-Id: <same site credential that created the event>
X-Localization-Signature: <signature>
```

```json
{"event_id":"cms-event-184","schema":"blun.cms-content-tombstone.v1","site_id":"public-site","source_id":"homepage.hero","source_sequence":184,"tombstone_id":"cms-tombstone-184","website_version":"website-2026-08-29.1"}
```

The service accepts this request only after the exact publication has a valid,
signed `succeeded` acknowledgement. It binds the immutable tombstone to the
original tenant key, event, site, source generation, website version,
publication delivery ID, publication-payload hash, plan, and complete sorted
locale set. It never copies target text into the tombstone payload.

The first request returns `202 Accepted`; an exact replay returns `200 OK` and
the same deterministic delivery ID. Unknown, unpublished, cancelled,
differently bound, or colliding requests fail closed. A durable lease-based
outbox sends `blun.cms-localization-tombstone.v1`; the CMS must return the exact
signed `blun.cms-localization-tombstone-ack.v1` acknowledgement with status
`deleted`. Network and retryable acknowledgement failures use the bounded
outbox retry policy. Lifecycle and health report `deleting`,
`deletion_failed`, or `deleted`; accepting or retrying a tombstone does not call
a model or remove the immutable publication audit record.

## Discover the active localization contract

A CMS can discover the exact runtime contract before creating work. This avoids
copying a locale list or quality-profile version into an integration where it
can silently become stale. The read uses its own signed purpose and does not
require an existing event:

```http
POST /v2/localization/capabilities HTTP/1.1
Content-Type: application/json; charset=utf-8
Content-Length: <exact UTF-8 byte count>
X-Localization-Signature-Algorithm: <configured algorithm>
X-Localization-Key-Id: <credential identifier>
X-Localization-Signature: <signature>
```

```json
{"request_id":"capabilities-9","requested_at":1788955200,"schema":"blun.cms-localization-capabilities-request.v1"}
```

The request signature and five-minute freshness window prevent an old or
different API request from being replayed for discovery. The response contains
the active change, plan, job, publication, and commercial-profile versions;
the ordered quality phases; accepted content types; the official EU language
source; the default target-selection rule; and all 24 exact BCP-47 locale
profiles. Every locale entry includes its EU code, language code, native name,
script, direction, quality-profile version, and quality-profile SHA-256 digest.
It does not expose the full profile instructions, credentials, customer text,
provider data, or mutable service state.

The nested `publication_http` object is the separately hashed, machine-readable
contract for the built-in outbound CMS adapter. It declares publication,
tombstone, and content-free health payload, request, acknowledgement, and
response schemas; the nested release-evidence schema; exact
success values; accepted JSON content types; at-least-once delivery; and the
three headers that bind every attempt to its delivery ID and payload hash. It
also declares the three health headers that bind a fresh probe ID and this
contract's digest. It does not advertise an active endpoint or credential, or
claim that a custom host publisher uses this adapter. A receiver can therefore
implement and test the
supported callback protocol without copying prose from the integration guide.
The transport and discovery manifest use the same constants, so schema or
header drift changes the digest or blocks discovery rather than producing a
partial contract.

CMS implementations can use
`integrations/website_localization_cms_receiver.py` as the fail-closed
publication, tombstone, and health reference receiver. For publication, the host
supplies the expected source generation, complete required-locale set, content
type, and commercial profile. For deletion, it supplies the exact acknowledged
publication delivery and payload hash as well as the complete locale set. The
receiver validates those bindings, release evidence where applicable, hashes,
headers, expiry times, and the publisher signature before invoking the host's
atomic, idempotent commit or delete callback. It returns a signed `accepted` or
`deleted` acknowledgement only after the callback confirms the exact delivery
ID and payload hash. The host continues to own authentication, key provisioning,
durable transactions, and target-text log redaction.

For the content-free health challenge, the host supplies an authentication
callback, the exact active `publication_http.sha256`, a health callback, and the
same acknowledgement authority used by the publisher adapter. The receiver
strictly parses and binds the probe before authentication, authenticates before
comparing deployment state or calling health logic, and accepts only an exact
`healthy` receipt for the probe ID and contract digest. It signs that same
binding only after the host confirms health. Authentication outages, wrong
contracts, malformed transport, private health errors, false receipts, and
signing failures cannot produce a healthy acknowledgement.

`CMSReceiverApplication` exposes these three receiver operations through one
provider-neutral WSGI callable. Its default mount is
`/v1/localization/callback`, matching the built-in publisher's one configured
endpoint for publication, tombstone, and health requests. The application
requires an exact query-free HTTPS `POST`, canonical UTF-8 JSON, explicit
bounded `Content-Length`, non-chunked framing, and host authentication. It then
dispatches only the three published outer schemas. Publication and tombstone
expectation resolvers see a request only after its payload, binding headers,
hashes, and publisher signature have passed verification; host writes retain
the later exact-receipt requirement. WSGI errors use the content-free
`blun.cms-localization-receiver-error.v1` envelope with only a stable code and
retry decision. A TLS terminator must set `wsgi.url_scheme` from trusted proxy
configuration, and the host must keep authorization headers and verified target
text out of access logs.

`DurableCMSReceiverStore` in
`integrations/website_localization_cms_receiver_store.py` supplies a complete
SQLite reference implementation for the five stateful host callbacks. It uses
one dedicated host-owned connection. The CMS registers each monotonic current
source before delivery and explicitly pre-registers a tombstone against the
exact active publication before deletion. Commit and delete recheck those
bindings inside `BEGIN IMMEDIATE`, so the earlier resolver lookup cannot race a
source change. Replays are bound to immutable delivery and payload hashes, a
replacement preserves the last-known-good bundle until its complete locale set
commits. The successful replacement transaction then securely deletes the
superseded payload and locale rows while retaining only content-free replay
evidence; any cleanup failure rolls the switch back to the previous active
bundle. Explicit deletion follows the same content-minimizing rule. The store's
health callback validates schema, SQLite integrity, canonical payloads, locale rows, active pointers, and
tombstone state before confirming the probe. Source and tombstone expectations
carry separate canonical hashes, so a syntactically valid field substitution
also blocks. `read_active_bundle` is for trusted
CMS rendering code only. It requires the complete trusted publication
expectation and returns text only when the active signed payload matches its
event, website version, plan, source revision, sequence and hash, exact locale
set, content type, and commercial profile. Site/source-only lookup is rejected,
so a retained last-known-good bundle cannot be attached to a newer generation.
The method contains target text and is not part of any public status response.
A WSGI worker process owns its store connection; request
threads may share it only through the composed runtime's serialized boundary.
Multiple worker processes may open distinct
connections to the same file and rely on the transactional writer lock.

`open_durable_cms_receiver` in
`integrations/website_localization_cms_receiver_runtime.py` is the reference
composition root. It validates the complete receiver configuration before it
opens SQLite, requires distinct publication and acknowledgement authority
objects, rejects SQLite URI configuration, and returns one owner for the store,
trusted registration and rendering methods, connection lifetime, and WSGI
callable. A failed preflight creates no database; a later initialization failure
closes the connection and never returns a partial application. Instantiate it
once after each WSGI worker starts and call `close` during that worker's orderly
shutdown. Inside one worker, a shared reentrant lock serializes all eight store
paths: both expectation resolvers, commit, delete, health, trusted registration,
and rendering. The composition root opens SQLite for cross-thread access only
behind that lock, so a multithreaded worker can share the WSGI callable without
racing transactions or reads. This does not make a pre-fork connection safe.
The runtime binds itself to its creator process and rejects inherited callbacks
and trusted host operations before lock acquisition; each worker process must
construct and own its own runtime after forking.

For a durable store, pass a canonical absolute POSIX database path. Its direct
parent must be a real service-owned directory. Every ancestor must be root- or
service-owned and non-shared-writable, apart from a root-owned sticky temporary
directory. The runtime atomically creates a missing database as `0600` and
accepts an existing one only when it is a service-owned, single-link regular
file with
that exact mode. Directory and file identity, permissions, ownership, and link
count are checked again before every callback or trusted host operation;
symlinks, hard links, path replacement, and permission drift fail closed.
`:memory:` is retained solely for ephemeral test composition.

The separate `api_contract` object lists all six tenant operations with their
exact path, `POST` method, request schema, response schema, and current
`enabled` state. A standalone host without approval and publication authorities
still advertises lifecycle and tombstone schemas but marks those operations
disabled, so a CMS can fail closed before submitting work. The object uses
`blun.website-localization-http-capabilities.v1` and carries its own SHA-256
over every other canonical field. Paths and schemas come from the same runtime
constants used for routing; they are not copied into a second configuration.

The nested `blun.website-localization-capabilities.v3` object carries a
`sha256` value over all its other canonical fields. Consumers can pin that
digest for a deployment and deliberately reconfigure when it changes. The
runtime rebuilds and validates the complete registry on every read; duplicate,
missing, noncanonical, or profile-mismatched entries return a fail-closed `503`
without a partial locale list.

Within it, `commercial_profile` is a separately hashed
`translate-native.commercial-capabilities.v2` object. Its nested and separately
hashed `review_summary_contract` defines the exact content-free result schema,
field set, verified/review-required state invariant, ten allowed ordered
review dimensions, complete-evidence hash semantics, and excluded sensitive
content. A CMS or independent-review adapter can validate targeted commercial
escalation without receiving project prices, brands, source/target text, spans,
or reviewer prose. Any registry or digest drift blocks the whole discovery
response rather than advertising a partial contract.

```json
{
  "capabilities": {
    "change_schema": "blun.cms-content-change.v2",
    "cancellation_schema": "blun.cms-content-cancellation.v1",
    "commercial_profile": {
      "profile": "translate-native.commercial.v2",
      "review_summary_contract": {
        "content_policy": {"project_brands": false, "project_prices": false, "reviewer_prose": false, "source_spans": false, "source_text": false, "target_spans": false, "target_text": false},
        "evidence_sha256": {"algorithm": "sha-256", "canonicalization": "utf-8-json-sort-keys-no-insignificant-whitespace", "covers": "complete-commercial-review-evidence"},
        "profile": "translate-native.commercial.v2",
        "required_fields": ["schema", "profile", "status", "review_required_dimensions", "evidence_sha256"],
        "result_schema": "translate-native.commercial-review-summary.v1",
        "review_required_dimensions": {"allowed": ["amount_currency", "discount_basis", "qualifiers", "tax_status", "billing_interval", "commitment", "renewal", "cancellation", "conditions", "offer_assignment"], "order": ["amount_currency", "discount_basis", "qualifiers", "tax_status", "billing_interval", "commitment", "renewal", "cancellation", "conditions", "offer_assignment"], "unique": true},
        "schema": "translate-native.commercial-review-summary-capabilities.v1",
        "sha256": "<sha256>",
        "statuses": {"review_required": {"requires_independent_review": true, "review_required_dimensions": "one-or-more"}, "verified": {"review_required_dimensions": "empty"}}
      },
      "schema": "translate-native.commercial-capabilities.v2",
      "sha256": "<sha256>"
    },
    "content_types": ["commercial", "cta", "documentation", "headline", "legal", "marketing", "seo", "ui"],
    "default_target_policy": "all-eu-official-locales-except-source-language",
    "eu_language_source": "https://european-union.europa.eu/principles-countries-history/languages_en",
    "job_schema": "blun.website-localization-job.v2",
    "locales": [{
      "direction": "ltr",
      "eu_code": "MT",
      "language": "mt",
      "locale": "mt-MT",
      "native_name": "Malti",
      "quality_profile_sha256": "<sha256>",
      "quality_profile_version": "eu-mt-MT-2026-09-1",
      "script": "Latn"
    }],
    "plan_schema": "blun.website-localization-plan.v2",
    "publication_http": {
      "binding_headers": [
        {"binding": "delivery_id", "name": "Idempotency-Key"},
        {"binding": "delivery_id", "name": "X-Localization-Delivery-Id"},
        {"binding": "payload_sha256", "name": "X-Localization-Payload-Sha256"}
      ],
      "delivery_semantics": "at-least-once",
      "health_binding_headers": [
        {"binding": "probe_id", "name": "Idempotency-Key"},
        {"binding": "probe_id", "name": "X-Localization-Probe-Id"},
        {"binding": "contract_sha256", "name": "X-Localization-Contract-Sha256"}
      ],
      "method": "POST",
      "operations": [{
        "acknowledgement_schema": "blun.cms-localization-publication-ack.v1",
        "acknowledgement_status": "accepted",
        "name": "publication",
        "payload_schema": "blun.cms-localization-publication.v3",
        "request_schema": "blun.cms-localization-publication-http.v1",
        "response_schema": "blun.cms-localization-publication-http-ack.v1"
      }],
      "request_content_type": "application/json; charset=utf-8",
      "release_evidence_schema": "blun.website-localization-release-evidence.v1",
      "response_content_types": ["application/json", "application/json; charset=utf-8"],
      "schema": "blun.cms-localization-publication-http-capabilities.v2",
      "sha256": "<sha256>"
    },
    "publication_schema": "blun.cms-localization-publication.v3",
    "quality_passes": ["target_native", "source_fidelity"],
    "schema": "blun.website-localization-capabilities.v3",
    "sha256": "<sha256>"
  },
  "api_contract": {
    "api_schema": "blun.website-localization-api.v2",
    "error_schema": "blun.website-localization-api.v2",
    "operations": [{
      "enabled": true,
      "method": "POST",
      "name": "lifecycle",
      "path": "/v2/localization/lifecycle",
      "request_schema": "blun.cms-localization-lifecycle-request.v1",
      "response_schema": "blun.cms-localization-lifecycle.v3"
    }],
    "schema": "blun.website-localization-http-capabilities.v1",
    "sha256": "<sha256>"
  },
  "request_id": "capabilities-9",
  "schema": "blun.website-localization-api.v2",
  "status": "CAPABILITIES"
}
```

The abbreviated example shows one locale, one inbound operation, one outbound
adapter operation, and only the commercial profile fields relevant to summary
discovery. A successful real response always contains the complete commercial
profile, all 24 locales, all six inbound operations, and all three outbound
operations, and otherwise blocks.

## Read per-locale progress

Status is a signed, short-lived POST rather than an unauthenticated identifier
in a URL:

```http
POST /v2/localization/status HTTP/1.1
Content-Type: application/json; charset=utf-8
Content-Length: <exact UTF-8 byte count>
X-Localization-Signature-Algorithm: <configured algorithm>
X-Localization-Key-Id: <same credential that created the event>
X-Localization-Signature: <signature>
```

```json
{"event_id":"cms-event-184","request_id":"status-7","requested_at":1788955200,"schema":"blun.cms-localization-status-request.v2","site_id":"public-site"}
```

Sign the canonical status object. `requested_at` must be within five minutes of
the service clock. The signed `site_id` and signature header's `key_id` must
match the stored event scope exactly, even when the verifier recognizes several
valid credentials. This prevents one valid tenant credential from probing
another tenant's event. A rotated deployment must retain the original verifier
identity for unfinished events or explicitly migrate them outside this module.

The read-only response echoes `request_id` and reports the bound site, website
version, source sequence, plan and job counts, plus one item per target locale.
Each item contains job/locale identity, state, attempts, retry timing, lease
expiry, stable error code, optional private-detail hash, and optional result
hash. The top-level `cancelled` flag records an accepted withdrawal.
`lease_expired: true` makes recoverable crashes visible. A succeeded queue
row is reloaded and hash-checked before it can appear successful.

If the signed event is durable but the process stopped before queue insertion,
`queue_recovery_pending` is `true`. Every required locale then reports
`awaiting_queue_resume` with the stable content-free reason
`cms.event.awaiting_queue_resume`; all real queue counts remain zero. The read
does not create jobs or invoke a provider. The supervised service separately
revalidates and resumes that exact event. Once queue insertion succeeds, the
flag becomes `false` and ordinary queue state replaces the synthetic status.

Status is not readiness. Valid independent evidence, signed per-locale
approvals, and the all-required-locales release gate remain authoritative.
Superseded events, wrong site or credential scope, missing work, altered queue
identity, or a corrupt result return a complete fail-closed error rather than a
partial optimistic status.

## Read verified end-to-end lifecycle

The tenant can separately ask whether the same event is cancelled, still
translating, awaiting signed approvals, ready, publishing, blocked, failed, or
confirmed as published. This operation has its own schema so a valid
queue-progress request cannot be replayed for a broader lifecycle read:

```http
POST /v2/localization/lifecycle HTTP/1.1
Content-Type: application/json; charset=utf-8
Content-Length: <exact UTF-8 byte count>
X-Localization-Signature-Algorithm: <configured algorithm>
X-Localization-Key-Id: <same credential that created the event>
X-Localization-Signature: <signature>
```

```json
{"event_id":"cms-event-184","request_id":"lifecycle-8","requested_at":1788955200,"schema":"blun.cms-localization-lifecycle-request.v1","site_id":"public-site"}
```

The signature, five-minute freshness window, site scope, and original key scope
are identical to the progress route. The distinct schema binds the signature to
this purpose. The composed runtime always supplies the approval and publication
verification authorities. A standalone `WebsiteLocalizationAPI` must supply
both authorities together; otherwise this route returns a fail-closed `503`.

A successful response uses
`blun.cms-localization-lifecycle.v3`. It contains the event, site, website
version, plan and source-sequence identifiers; aggregate queue counts; required,
approved and blocked locales; and an optional content-free delivery summary.
The lifecycle `status` is exactly one of:

- `cancelled`: the exact unpublished event has an accepted signed cancellation;
- `queue_recovery`: the event is durably accepted but its per-locale queue rows
  still await the supervised, idempotent recovery pass;
- `processing`: at least one required locale still has queue work;
- `localization_failed`: at least one required locale failed terminally;
- `awaiting_approval`: all locale results exist, but signed release evidence is
  missing;
- `ready`: every required locale has a current verified approval, but no signed
  CMS delivery exists yet;
- `publishing`: a valid signed delivery is pending, leased, or waiting for retry;
- `publication_blocked`: the prepared delivery can no longer be published, for
  example because an approval expired before acknowledgement;
- `publication_failed`: bounded delivery attempts ended terminally;
- `published`: the CMS returned the exact signed acknowledgement for the
  delivery.

The read path revalidates every successful queue result and signed approval,
then checks the complete stored publication envelope, signature, event, plan,
source revision and locale set. It does not prepare, claim, renew, retry, sign,
repair, or publish anything. A published acknowledgement stays published after
its former approval validity window ends; expiration before acknowledgement is
reported as `publication_blocked`.

```json
{"approved_locales":["fi-FI"],"blocked_locales":[],"delivery":{"attempts":0,"delivery_id":"blun-cms-delivery-…","last_error_code":null,"last_error_detail_hash":null,"lease_expired":false,"lease_expires_at":null,"max_attempts":5,"next_attempt_at":1788955201.0,"status":"pending"},"event_id":"cms-event-184","plan_id":"blun-l10n-plan-…","queue_counts":{"failed":0,"leased":0,"pending":0,"retry_wait":0,"succeeded":1},"request_id":"lifecycle-8","required_locales":["fi-FI"],"schema":"blun.cms-localization-lifecycle.v3","site_id":"public-site","source_sequence":42,"status":"publishing","website_version":"release-42"}
```

## Failure contract

Every failure has `Cache-Control: no-store` and contains no customer or provider
prose:

```json
{"error":"cms.status.scope_rejected","schema":"blun.website-localization-api.v2","status":"BLOCK"}
```

`401` covers invalid, expired, or wrong-scope signed requests; `409` covers
identity, source-sequence, supersession, cancellation, in-flight publication,
and legacy-ingress conflicts;
`413` and `415` cover body size and media type; `503` covers inconsistent or
unavailable durable state, including missing lifecycle authorities and invalid
release or delivery evidence, or an inconsistent capability registry. Other
invalid input returns `400`, and unexpected failures reduce to `api.internal`
with `500`. Clients may retry a transport failure or the exact signed change;
they must never modify a request under the same event identity.

Premortem: schema-v1 ingress could bypass source ordering, a valid credential
could enumerate another site's event, a timeout could duplicate locale work, a
corrupt queue could look complete, or a status response could leak customer
text. The v2-only endpoint, exact event/sequence idempotency, signed site and
original-key scoping, durable planner identities, full result revalidation, and
content-free responses keep those paths fail-closed.

Lifecycle premortem: a tenant could probe another site's release, queue success
could be mistaken for approval, an expired signature could remain apparently
ready, a corrupt outbox row could be skipped, or a status read could mutate a
lease. Purpose-bound signed requests, original-credential scope, verified
release readiness, complete signed-delivery revalidation, explicit blocked
states, and read-only regression checks keep those paths fail-closed.

Capability premortem: an integration could pin a stale locale list, a valid
signature could be replayed across purposes, a changed profile could retain an
old digest, a duplicate language could displace another EU language, or a
partial response could look authoritative. A separate fresh signed request,
canonical whole-object digest, exact 24-language registry validation, unique
locale/language/EU-code checks, and all-or-nothing response close those paths.

Cancellation premortem: a signed request could target another source revision,
another accepted key could withdraw a tenant's event, a crash could leave a
pending outbox active, or the endpoint could claim to retract a request already
in flight. Exact immutable bindings, original-key scope, one transactional
ledger/outbox transition, service eligibility filtering, and explicit leased
and published conflicts keep those paths fail-closed.
