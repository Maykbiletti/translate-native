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

The separate `api_contract` object lists all six tenant operations with their
exact path, `POST` method, request schema, response schema, and current
`enabled` state. A standalone host without approval and publication authorities
still advertises lifecycle and tombstone schemas but marks those operations
disabled, so a CMS can fail closed before submitting work. The object uses
`blun.website-localization-http-capabilities.v1` and carries its own SHA-256
over every other canonical field. Paths and schemas come from the same runtime
constants used for routing; they are not copied into a second configuration.

The nested `blun.website-localization-capabilities.v1` object carries a
`sha256` value over all its other canonical fields. Consumers can pin that
digest for a deployment and deliberately reconfigure when it changes. The
runtime rebuilds and validates the complete registry on every read; duplicate,
missing, noncanonical, or profile-mismatched entries return a fail-closed `503`
without a partial locale list.

```json
{
  "capabilities": {
    "change_schema": "blun.cms-content-change.v2",
    "cancellation_schema": "blun.cms-content-cancellation.v1",
    "commercial_profile": "translate-native.commercial.v2",
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
    "publication_schema": "blun.cms-localization-publication.v2",
    "quality_passes": ["target_native", "source_fidelity"],
    "schema": "blun.website-localization-capabilities.v1",
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

The abbreviated example shows one locale and one operation only; a successful
real response always contains all 24 locales and all six operations, and
otherwise blocks.

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
