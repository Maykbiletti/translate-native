# CMS localization webhook API v2

`WebsiteLocalizationAPI` is the provider-neutral WSGI ingress exposed by the
composed website-localization runtime. It accepts a signed current CMS change,
durably enqueues one job per required locale, and returns content-free progress.
It never calls a model, approves a translation, prepares a publication, or
returns source or target prose.

## Host setup

Use `runtime.cms_api`, or construct `WebsiteLocalizationAPI` with the exact CMS
bridge, event verifier, clock, and queue attempt limit. Mount the callable behind
a production WSGI server and TLS terminator. HTTPS is required by default. Only
a trusted proxy may derive `wsgi.url_scheme`; never trust an arbitrary forwarded
header. The host owns keys, credential-to-site provisioning, network access,
rate limits, timeouts, database backup, and request-log redaction.

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

```json
{"event_id":"cms-event-184","inserted_jobs":23,"job_count":23,"plan_id":"blun-l10n-plan-…","schema":"blun.website-localization-api.v2","status":"enqueued"}
```

The response contains identifiers and counts only. Acceptance is not quality
approval or publication readiness.

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
hash. `lease_expired: true` makes recoverable crashes visible. A succeeded queue
row is reloaded and hash-checked before it can appear successful.

Status is not readiness. Valid independent evidence, signed per-locale
approvals, and the all-required-locales release gate remain authoritative.
Superseded events, wrong site or credential scope, missing work, altered queue
identity, or a corrupt result return a complete fail-closed error rather than a
partial optimistic status.

## Failure contract

Every failure has `Cache-Control: no-store` and contains no customer or provider
prose:

```json
{"error":"cms.status.scope_rejected","schema":"blun.website-localization-api.v2","status":"BLOCK"}
```

`401` covers invalid, expired, or wrong-scope signed status requests; `409`
covers identity, source-sequence, supersession, and legacy-ingress conflicts;
`413` and `415` cover body size and media type; `503` covers inconsistent or
unavailable durable state. Other invalid input returns `400`, and unexpected
failures reduce to `api.internal` with `500`. Clients may retry a transport
failure or the exact signed change; they must never modify a request under the
same event identity.

Premortem: schema-v1 ingress could bypass source ordering, a valid credential
could enumerate another site's event, a timeout could duplicate locale work, a
corrupt queue could look complete, or a status response could leak customer
text. The v2-only endpoint, exact event/sequence idempotency, signed site and
original-key scoping, durable planner identities, full result revalidation, and
content-free responses keep those paths fail-closed.
