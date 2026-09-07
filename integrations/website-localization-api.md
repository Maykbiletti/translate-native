# CMS localization webhook API v1

`WebsiteLocalizationAPI` is a provider-neutral WSGI adapter for trusted CMS and
website backends. It accepts a signed complete content-change event and enqueues
one deterministic job per requested locale. It does not call an LLM, approve a
translation, prepare a publication, or return source or target text.

## Host setup

Construct `LocalizationQueue`, `LocalizationReleaseStore` and
`WebsiteLocalizationCMSBridge` with their own SQLite connections. Inject the
bridge and a `CMSMessageAuthority` verifier into `WebsiteLocalizationAPI`.
Signing keys remain outside this module. The verifier chooses the supported
algorithm and resolves `key_id`; never use an algorithm named by the request
without enforcing an allowlist in that trusted verifier.

Mount the WSGI callable behind a production HTTP server and TLS terminator. The
adapter requires `wsgi.url_scheme == "https"` by default. A trusted reverse
proxy must set that value from its own connection metadata, not from an
untrusted forwarded header. Apply network authentication, rate limits, request
timeouts and database backup policy at the host. Do not enable interactive
debug pages or log request bodies, signature values, source text or target text.

`require_https=False` exists only for a protected loopback or test transport;
it is not an Internet deployment mode.

## Create or resume localization work

```http
POST /v1/localization/changes HTTP/1.1
Content-Type: application/json; charset=utf-8
Content-Length: <exact UTF-8 byte count>
X-Localization-Signature-Algorithm: <configured algorithm>
X-Localization-Key-Id: <configured key identifier>
X-Localization-Signature: <signature>
```

The body is the complete `blun.cms-content-change.v1` object documented by
`website_localization_cms.py`. Its `localization` field uses the planner input
contract. Omit `target_locales` to select every supported EU locale other than
an EU source locale; otherwise provide the exact allowed locales. The trusted
CMS supplies content type, source revision, glossary/policy versions and the
provider/model/software identity. The generating model must not choose these
fields.

Sign the UTF-8 bytes of canonical JSON: object keys sorted, no insignificant
spaces, native Unicode characters unescaped, and no NaN or Infinity. This is
the output of the CMS module's `_canonical_json(event)`. The transmitted JSON
may use different insignificant whitespace; after strict parsing, the adapter
reconstructs the same canonical payload for verification. Duplicate keys,
UTF-8 BOM, invalid UTF-8, nonfinite numbers, unknown fields, chunked transfer,
missing or truncated length and bodies over 4,000,000 bytes are rejected before
work is accepted. The production WSGI server and proxy must enforce HTTP message
framing and reject request smuggling; the application never reads beyond the
declared `Content-Length`.

Example success for new work:

```json
{"event_id":"cms-event-184","inserted_jobs":23,"job_count":23,"plan_id":"blun-l10n-plan-…","schema":"blun.website-localization-api.v1","status":"enqueued"}
```

Newly inserted work returns `202 Accepted`. Replaying the exact signed event
returns `200 OK` with `inserted_jobs: 0`; this resumes safely after an uncertain
client timeout. Reusing an `event_id` with different canonical content returns
`409 Conflict`. A valid response contains identifiers and counts only, never
customer content or signatures.

## Read per-locale progress

Use a signed short-lived JSON request rather than an unauthenticated URL:

```http
POST /v1/localization/status HTTP/1.1
Content-Type: application/json; charset=utf-8
Content-Length: <exact UTF-8 byte count>
X-Localization-Signature-Algorithm: <configured algorithm>
X-Localization-Key-Id: <configured key identifier>
X-Localization-Signature: <signature>
```

```json
{"event_id":"cms-event-184","request_id":"status-7","requested_at":1788775200,"schema":"blun.cms-localization-status-request.v1"}
```

Sign this request with the same canonical-JSON rules. `requested_at` must be
within five minutes of the API clock. Use a new unpredictable `request_id` for
each call; the server echoes it so clients can bind the response to their
request. The operation is read-only, so an exact replay inside the validity
window cannot mutate work.

A successful response reports counts plus one entry per required locale. Each
entry includes job/locale identity, state, attempts, retry or lease timing, a
stable error code, an optional hash of private error detail, and an optional
result hash. `lease_expired: true` makes a crashed worker visible without
silently changing queue state. No source, target, model output, signature or
raw provider error is returned.

Status is not readiness: `succeeded` means the worker stored an integrity-valid
result, not that its quality receipt or signed approval is valid. The existing
release store remains authoritative for publication. Before reporting status,
the bridge revalidates the stored signed event, every plan/job/locale identity,
the queue count, and every stored successful result. Missing events return
`404`; inconsistent or tampered queue state returns `503` rather than a partial
or optimistic response.

## Failure contract

Failures use this content-free form and `Cache-Control: no-store`:

```json
{"error":"cms.event.signature_rejected","schema":"blun.website-localization-api.v1","status":"BLOCK"}
```

`401` denotes a missing, rejected or expired signed request, `409` an idempotency collision,
`413` an oversized body, `415` the wrong media type, and `503` unavailable or
invalid queue state. Other invalid events return `400`; unexpected failures
return only `api.internal` with `500`. Clients may retry transport failures and
the exact event safely. They must never change the event under the same ID.

Acceptance is not publication readiness. Workers must still complete the
separate target-only and source-aware checks, deterministic integrity guards,
quality receipt verification, signed per-locale approvals and all-required-
locales readiness gate before the CMS publication outbox can emit anything.
