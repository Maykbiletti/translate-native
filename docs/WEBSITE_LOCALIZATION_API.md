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

## Source-side reference client

`CMSLocalizationHTTPClient` in
`integrations/website_localization_cms_client.py` implements the sending side
for all six routes. Configure one origin containing only an HTTPS scheme and
authority, a callback that supplies host-owned authentication headers, and an
authority whose `sign(bytes)` method signs the exact canonical request bytes.
Credentials in the URL, base paths, query strings, fragments, redirects, and
authentication headers that collide with protocol headers are rejected.

Each method performs one HTTP attempt. The client does not retry: the CMS host
must decide whether and when to repeat an immutable event, cancellation,
tombstone, or short-lived read request. `submit_change`, `cancel`, and
`request_tombstone` accept complete versioned objects; `status`, `lifecycle`,
and `capabilities` create fresh signed read requests from the configured clock
and request-ID source. Caller mappings are copied through canonical
native-Unicode JSON before signing and are never mutated.

Successful responses require canonical bounded JSON, an allowed status code,
the exact response schema, and the originating request, event, and site
bindings. Capability responses additionally recompute both advertised hashes
and verify the exact ordered operation names, methods, paths, request schemas,
response schemas, and enabled booleans. Rehashing a substituted endpoint is
therefore insufficient. Stable server failures retain their content-free error
code and derive retryability from HTTP status; redirects and invalid bindings
fail closed.

### Durable change dispatch

`DurableCMSChangeDispatcher` in
`integrations/website_localization_cms_dispatch.py` is the optional persistent
outbox in front of `CMSLocalizationHTTPClient.submit_change`. The host supplies
and secures a SQLite connection, enqueues the complete versioned change before
returning from its content-change handler, and runs `run_once` from a supervised
worker. Different worker processes must use different connections to the same
database.

The outbox binds `event_id` to canonical native-Unicode payload bytes, their
SHA-256, and a fixed attempt ceiling. An exact enqueue is idempotent; changing
the event, payload, or attempt policy under the same ID is a collision. Claiming
is transactional, carries an expiring worker token, and permits one HTTPS call.
The lease must outlive the configured client timeout. Retryable client failures
use capped exponential backoff; permanent failures and the attempt ceiling are
terminal. A lease lost after remote acceptance replays the same event after
expiry, relying on the server's existing exact event idempotency instead of
inventing a second identity.

`status` and `health` are content-free. They return event and payload hashes,
attempt state, stable error codes, remote acknowledgement metadata, counts, and
expired-lease indicators, never source text, credentials, signatures, or raw
transport errors. Schema, payload, hash, lease, acknowledgement, and state
inconsistencies block rather than being repaired optimistically.

### Durable lifecycle monitoring

`DurableCMSLifecycleMonitor` in
`integrations/website_localization_cms_lifecycle_monitor.py` closes the
source-side loop after a successful change dispatch. Registration binds the
original canonical change hash to the returned event, site, plan, website
version, source sequence, and exact job count in a separate canonical binding
hash. A changed generation or a
dispatch that has not succeeded cannot be registered under the same event.

Each leased attempt invokes `CMSLocalizationHTTPClient.lifecycle` without a
caller-supplied request ID. The secure client therefore creates and signs a
fresh, purpose-bound request for every poll; an expired read request is never
stored for replay. Verified nonterminal states are polled again at the fixed
monitor interval. Retryable transport or service failures use capped
exponential backoff and a fixed consecutive-failure ceiling, while a successful
poll resets that failure streak. Permanent errors, malformed or
generation-mismatched responses, exhausted failures, and corrupted state stop
the event fail-closed.

Transactional expiring leases coordinate separate process connections and
recover a poll abandoned by a crashed worker. The monitor stores no source or
target text, credentials, signatures, or response prose. Its durable snapshot
contains only identities, locale names, counts, stable error codes, delivery
state, and the canonical lifecycle-response hash. `status` distinguishes
ongoing observation from verified terminal states; `health` remains blocked
for local monitor failure, expired ownership, or terminal localization,
publication, or deletion failure. A change already acknowledged as cancelled
or superseded becomes terminal without an unnecessary lifecycle request.

### Durable cancellation and tombstone dispatch

`DurableCMSRemovalDispatcher` in
`integrations/website_localization_cms_removal_dispatch.py` is the persistent
source-side outbox for removal. Enqueue the complete cancellation when content
must stop before publication, or the complete tombstone only when an
acknowledged publication must be deleted. The dispatcher preserves these as
distinct operations and invokes `CMSLocalizationHTTPClient.cancel` or
`request_tombstone` accordingly; it never guesses which removal phase applies.

The durable identity is `(operation, cancellation_id)` or
`(operation, tombstone_id)`, bound to the event, complete canonical request
bytes, SHA-256, and fixed attempt ceiling. Exact replay is idempotent; changed
website version, event, source ID, sequence, payload, or retry policy under that
identity is rejected. Transactional leases coordinate separate worker
connections. A lost lease after remote acceptance repeats the same immutable
request, so the service's cancellation or tombstone ledger converges without a
second logical deletion.

One `run_once` performs exactly one secure client operation. Retryable failures
enter capped exponential backoff, permanent failures and exhausted attempts
become terminal, and malformed acknowledgements fail closed. Content-free
status and health retain only operation/request/event IDs, hashes, attempt
state, stable errors, remote delivery identity and status, counts, and lease
times. They retain no website text, credentials, signatures, or private error
detail. Database, payload, claim, or response inconsistency blocks before a
network call.

### Coordinated source-CMS service

`CMSLocalizationSourceService` in
`integrations/website_localization_cms_source_service.py` composes the change
outbox, removal outbox, and lifecycle monitor into the complete source-side
worker. The host supplies three distinct durable SQLite connections, one
configured `CMSLocalizationHTTPClient`, stable worker identities, and a clock.
`enqueue_change` and `enqueue_removal` persist an immutable request before the
host acknowledges its own content change. `run_once` advances at most one
network operation; `run_forever` adds bounded active, idle, and blocked sleeps
without taking ownership of process signals or database connections.

The order is deliberate: due cancellations and tombstones run first, then a
locally missing lifecycle registration is repaired, then one new change is
sent, and only then is one lifecycle read performed. After a successful change
response, the service registers its exact event, plan, job count, website
generation, and canonical payload hash immediately. If the process exits after
remote acceptance or after the separate registration commit, the next tick
reconciles the two durable stores idempotently before any further change or
lifecycle network call. It never invents a new event identity or treats an
unregistered acknowledgement as completed monitoring.

The three underlying token-bound leases remain the concurrency authority, so
multiple supervised processes may use separate connections to the same three
database files. The service validates all dependencies, lease/timeout ordering,
worker IDs, connections, and delays before creating schemas. A conflicting
dispatch-to-lifecycle binding blocks the whole tick before network access.
`health` verifies all three stores plus every successful dispatch registration
and returns only counts, stable codes, and a pending-registration count. An
acknowledged change awaiting that local handoff is explicitly `degraded`; a
store failure or conflicting binding is `blocked`. Tick and health payloads
never contain website text, provider responses,
credentials, signatures, or exception messages.

Premortem: an acknowledgement can be lost between two local commits, a removal
can be starved by ordinary changes, a retry can duplicate logical work, or an
exception can leak customer prose. Registration reconciliation, removal-first
scheduling, the existing immutable IDs and leases, one external operation per
tick, and stable error reduction close those paths. Regression tests cover both
crash boundaries, restart recovery without resend, operation priority,
binding tampering, private exception redaction, health, loop delays, and
pre-schema configuration rejection.

### Durable source-CMS runtime

`open_durable_cms_source` in
`integrations/website_localization_cms_source_runtime.py` is the production
composition root for the coordinated source service. The host supplies three
canonical absolute SQLite paths, one already configured provider-neutral CMS
HTTP client, and stable worker IDs. The runtime validates the entire service
configuration in memory before it creates any persistent schema, opens and
owns three separate connections, and exposes the same `enqueue_change`,
`enqueue_removal`, `run_once`, `run_forever`, and `health` lifecycle through a
single process-bound object. It is a context manager and repeated `close` is
safe; all later work blocks after close.

Each filesystem database must be a regular owner-owned file with exactly one
link and mode `0600` beneath a private, non-aliased directory chain. URI paths,
relative paths, path reuse, symlinks, hard links, permissive files or parents,
and path identity replacement fail closed. The runtime rechecks all three
identities before and after every state transition, serializes access from
threads inside one process, and checks process ownership before acquiring its
lock. A pre-fork copy therefore cannot inherit SQLite connections or a locked
thread state. Multi-process supervisors instead construct one runtime after
each fork; independent connections then coordinate through the existing
transactional leases.

The runtime deliberately does not read environment variables, credentials, or
configuration files and does not own process signals. Authentication and the
request-signing authority stay inside the supplied `CMSLocalizationHTTPClient`;
their values, database paths, website content, and private exception messages
never enter the runtime representation or stable failure codes. A deployment
may therefore choose its own secret manager and supervisor without weakening
the provider-neutral contract.

### Authenticated source-CMS ingress

`integrations/website_localization_cms_source_http.py` provides the optional
WSGI boundary exposed as `runtime.http` when `open_durable_cms_source` receives
an `http_authenticator`. Mount it behind a production WSGI server and a trusted
TLS terminator. Construct the runtime after the worker process forks, and run
`runtime.run_forever` in the supervised background worker that owns the same
runtime. No HTTP route advances a lease or performs a network call.

The exact routes are:

| Method | Path | Required scope | Purpose |
| --- | --- | --- | --- |
| `POST` | `/v1/localization/source/changes` | `source-change:write` | Persist one complete signed CMS change |
| `POST` | `/v1/localization/source/removals` | `source-removal:write` | Persist one cancellation or tombstone |
| `GET` | `/v1/localization/source/health` | `source-health:read` | Read aggregate content-free health |

Change requests use
`blun.cms-source-change-enqueue-request.v1`; removal requests use
`blun.cms-source-removal-enqueue-request.v1`. Both contain the exact downstream
payload plus an explicit `max_attempts` from 1 through 20. Successful responses
return HTTP `202` with the canonical request identity, payload SHA-256, durable
state, attempt count, and retry ceiling. Replaying identical bytes converges on
the same durable item. Reusing an identity with different content or policy
returns HTTP `409` and does not alter stored work.

Before parsing JSON or touching SQLite, the application calls the host-supplied
authenticator with this content-free request:

```json
{
  "schema": "blun.cms-source-runtime-http-auth-request.v1",
  "method": "POST",
  "path": "/v1/localization/source/changes",
  "headers": [["authorization", "<host credential>"]],
  "body_sha256": "<sha256 of exact request bytes>"
}
```

The authenticator returns exactly
`blun.cms-source-runtime-principal.v1` with `principal_id`, `credential_id`,
`credential_version`, and the one route-specific `scope`. The runtime does not
interpret credentials and never stores them. Operators should give health and
write routes distinct credentials. The WSGI server remains responsible for
rejecting ambiguous wire-level HTTP before constructing the WSGI environment.

Requests require an exact query-free HTTPS route, fixed `Content-Length`, UTF-8
JSON, and no transfer encoding. Health accepts no body or content type. Every
response sets `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, and
`Referrer-Policy: no-referrer`. Errors use
`blun.cms-source-runtime-http-error.v1` and never echo a body, header, path,
credential, exception, source string, or target string.

Premortem: an invalid deployment could create state before discovering a bad
worker or timeout; two paths could alias one database; a pre-fork service could
reuse a vanished parent's lock; or a permission change could redirect the next
operation. In-memory configuration preflight, distinct canonical paths and
identities, process ownership checks before locking, per-call path guards, and
durable multi-process leases close those paths. Regression tests cover restart
persistence, owner-only creation, unsafe paths, changed permissions, close and
fork behavior, 24 concurrent thread ticks, and two independently constructed
workers converging on one change dispatch.

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
