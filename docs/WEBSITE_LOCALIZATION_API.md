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
bindings. Capability responses additionally recompute both advertised hashes,
verify the exact ordered operation names, methods, paths, request schemas,
response schemas, and enabled booleans, and compare the complete v5 capability
shape, commercial profile, 24 locale bindings, and commercial rendering
registry with the installed canonical contract. Rehashing a substituted
endpoint or registry is therefore insufficient.

Production hosts can also pass constructor-fixed
`capabilities_sha256` and `commercial_rendering_registry_sha256` deployment
pins. Each must be a lowercase 64-character SHA-256 digest; malformed
configuration is rejected before transport. A valid response that does not
match either configured pin fails after the single HTTP attempt with the
distinct, non-retryable `capabilities_pin_mismatch` or
`commercial_rendering_registry_pin_mismatch` code. Rotate a pin only as an
explicit deployment change after installing and validating the corresponding
client contract. Stable server failures retain their content-free error code
and derive retryability from HTTP status; redirects and invalid bindings fail
closed.

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

Set `capability_preflight=True` for a pinned production startup. It is
mandatory whenever `http_authenticator` exposes the runtime over HTTP. The
runtime then requires both constructor-fixed client pins described above and performs
one signed capability request after all in-memory configuration and existing
path checks, but before creating any database file. Missing pins, a network or
contract failure, and either pin mismatch leave all three paths absent and
return only a stable content-free failure code. `capability_binding()` reports
the two verified public digests or the explicit `not_configured` state. Every
later runtime transition rechecks that the client still exposes the exact
verified pair before reading or writing persistent state or making another
network request. This startup proof is deliberately not a hidden retry or a
claim that the remote service will remain available indefinitely; supervisors
must construct a new pinned runtime after an approved contract deployment.

Pinned startup also writes an independent canonical binding row into the
change, removal, and lifecycle databases. Each row binds the database role,
complete capability digest, commercial rendering-registry digest, and a
derived binding digest. Existing rows are validated before the service may add
or alter any queue schema. A restart may reuse only the same exact binding;
swapped database files and another capability generation block with stable
content-free errors. An unbound database can be adopted only when every
existing source queue is empty. This permits a safe first pinned deployment
without allowing pending legacy work to cross the policy boundary. If a crash
leaves only part of an otherwise matching empty set unbound, the next startup
converges the missing binding. `capability_binding()` uses its v2 response to
report all three verified database roles, and every later state transition
revalidates the canonical rows before queue access.

### Durable terminal notification callback

Polling remains sufficient, but a deployment can pass `terminal_notifier` and
`notification_worker_id` to `open_durable_cms_source` or
`open_hosted_cms_source`. Both values are required together. After the durable
lifecycle monitor verifies a terminal state, the service first stores one
`blun.cms-source-terminal-notification.v1` object in the same protected
lifecycle database. It contains only:

- `notification_id`, `event_id`, `site_id`, and `plan_id`;
- `website_version`, `source_sequence`, and `job_count`;
- `change_sha256` and `lifecycle_binding_sha256`;
- `terminal_status` and `lifecycle_sha256`.

`lifecycle_sha256` is present for a signed lifecycle response and is `null`
only when the original dispatch acknowledgement already made `cancelled` or
`superseded` terminal. Source text, target text, locale prose, credentials,
provider responses, and private errors are never copied into this callback.

The host callback must atomically and idempotently record the notification and
return exactly:

```json
{
  "schema": "blun.cms-source-terminal-notification-ack.v1",
  "notification_id": "terminal-<sha256>",
  "event_id": "cms-event-184",
  "site_id": "public-site",
  "status": "accepted",
  "notification_sha256": "<sha256>"
}
```

Returning normally with any other object is a terminal protocol failure. A
callback may raise `TerminalNotificationFailure(code, retryable=True)` only
for a content-free transient condition; retries use the same immutable
notification identity, an expiring lease, bounded exponential backoff, and
the configured `max_notification_attempts`. Unknown exceptions are reduced to
`terminal_notification.callback_failure` and fail closed. Notification health
is included in aggregate service health and therefore in hosted readiness.
Disabling the optional notifier preserves the existing authenticated status
polling contract and does not create notification state.

For a remote backend, pass an
`HTTPTerminalNotifierAdapter` from
`integrations/website_localization_cms_terminal_notification_http.py` as the
`terminal_notifier`. It sends the canonical notification object itself as the
request body to one configured HTTPS URL. Loopback HTTP is available only by
explicit test/development opt-in. Redirects are never followed, and the adapter
performs exactly one transport attempt; the durable notification outbox alone
decides whether and when to retry.

Each request reserves these exact transport headers:

- `Content-Type: application/json; charset=utf-8`;
- `Accept: application/json`;
- `Idempotency-Key: <notification_id>`;
- `X-Localization-Terminal-Notification-Id: <notification_id>`;
- `X-Localization-Terminal-Notification-Sha256: <body_sha256>`.

The host-supplied authentication callback receives a fresh copy of this
content-free request before any network access:

```json
{
  "schema": "blun.cms-source-terminal-notification-http-auth.v1",
  "method": "POST",
  "origin": "https://cms.example.test",
  "path": "/v1/localization/terminal-notifications",
  "notification_id": "terminal-<sha256>",
  "event_id": "cms-event-184",
  "site_id": "public-site",
  "body_sha256": "<sha256 of exact canonical request bytes>"
}
```

It may return deployment-specific authentication or signature headers, but it
cannot replace reserved framing, idempotency, or binding headers. Empty,
duplicated, injected, oversized, or reserved authentication headers block
before transport. The receiver returns the exact acknowledgement documented
above with HTTP `200` and JSON UTF-8 content type. A `3xx` response is a
permanent redirect failure; `408`, `425`, `429`, and `5xx` statuses plus network
failures are retryable by the outbox. Other statuses and malformed or
cross-bound successful responses are permanent protocol failures. Response
bodies and private exceptions are never copied into durable error state.

#### Reference terminal-notification receiver

`integrations/website_localization_cms_terminal_notification_receiver.py`
provides the matching provider-neutral WSGI endpoint. Construct one
`DurableCMSTerminalNotificationInbox` around a worker-owned SQLite connection,
then mount `CMSTerminalNotificationReceiverApplication` at the configured path.
For a multithreaded WSGI worker, open that connection with
`check_same_thread=False`; the inbox serializes access. Do not construct it before
forking: its process binding deliberately blocks inherited connections.

The receiver accepts only canonical UTF-8 JSON sent over HTTPS with the exact
content type, content length, idempotency key, notification ID, and notification
SHA-256 headers documented above. Before storage, it calls the host verifier as
`authenticate(authentication_context, normalized_headers)`. The first argument
is exactly the content-free object supplied to the sender's authentication-header
provider. The verifier must return:

```json
{
  "schema": "blun.cms-source-terminal-notification-principal.v1",
  "principal_id": "website-cms",
  "credential_id": "cms-key",
  "credential_version": "v1",
  "scope": "terminal-notification:write",
  "site_id": "public-site"
}
```

The returned `site_id` must equal the notification site. The receiver stores the
exact body and its hash in one SQLite transaction before returning the existing
`blun.cms-source-terminal-notification-ack.v1` acknowledgement. An exact replay
returns that same acknowledgement and preserves the original `received_at`.
Changed bytes under an existing event, notification, or payload identity return
HTTP `409`; malformed or wrongly bound requests are permanent `4xx` failures,
while unavailable authentication, damaged storage, and unexpected internal
failures return retryable `503`. Every error body is content free.

For deployment, prefer
`integrations/website_localization_cms_terminal_notification_receiver_runtime.py`.
Its `open_durable_terminal_notification_receiver` factory takes a canonical
absolute database path, the host verifier, exact HTTPS origin, and optional path
and clock. It validates the full HTTP boundary before opening SQLite, creates a
missing file exclusively with mode `0600`, and rejects unsafe parents, symlinks,
hard links, special files, permissive modes, and path replacement. Construct one
runtime after each WSGI worker forks; inherited runtimes report
`foreign-process` and block without entering a possibly inherited lock.

The runtime itself is the WSGI application. It serializes request execution with
close, rechecks the pinned database identity before every operation, and owns the
connection until `close()` or context-manager exit. `status(event_id)` returns
only the verified content-free receipt binding. `health()` checks SQLite
integrity and every stored notification and processing row, then returns only
content-free status, runtime state, counts, due work, expired leases, and
terminal failures. Closed, exchanged, damaged, or foreign-process runtimes
return content-free HTTP `503` and require a new worker runtime; they never
attempt repair or invent acknowledgement state.

#### Durable CMS-side processing

Schema V2 of the receiver inbox creates one processing row in the same
transaction as every newly received notification. The HTTP acknowledgement is
therefore impossible unless both the immutable receipt and its discoverable
host work item have committed. When a V1 database opens, the receiver validates
the exact old schema and every stored notification, creates the processing
ledger, backfills one pending row per receipt, and advances the schema version
inside one transaction. Any altered input rolls the migration back.

Call `runtime.process_next(callback, worker_id)` to advance at most one due
notification. The callback receives a fresh mapping of the exact content-free
terminal notification and must return:

```json
{"event_id":"cms-event-184","notification_id":"terminal-…","notification_sha256":"…","schema":"blun.cms-terminal-notification-processing-ack.v1","site_id":"public-site","status":"processed"}
```

The claim binds the worker ID, random lease token, attempt number, deadline,
notification identity, event, site, terminal status, and exact payload hash.
Completion revalidates those bindings and rejects expired or replaced claims.
A host may raise `TerminalNotificationProcessingFailure` with one stable error
code and an explicit retry decision. Retryable failures wait using the
configured bounded exponential delay; permanent failures and exhausted attempts
remain durably failed and make health `blocked`. Unexpected exceptions are
reduced to `processing_callback_failure`; their messages are never stored.

The runtime serializes processing with request handling and shutdown. A crash
leaves the lease durable; the next worker recovers it after expiry and a stale
owner cannot complete it. Consequently, the host callback must apply its own
state change idempotently under `notification_id`: a crash after the host commit
but before the processing completion commit deliberately repeats the same exact
notification. Runtime health now includes content-free processing counts, due
work, expired leases, and terminal failures.

For a single-process WSGI deployment, use
`open_hosted_durable_terminal_notification_receiver`. It preflights the handler,
worker ID, lease, and all loop delays before creating the database, then starts
one process-owned, non-daemon worker. The runtime accepts HTTP notifications
only while that managed worker is running and the inbox remains healthy.
`worker_readiness()` returns the content-free
`blun.cms-terminal-receiver-readiness.v1` object; supervisors must remove the
instance from service whenever its status is `not_ready`.

Idle, active, and blocked outcomes use separate interruptible delays. A private
loop exception becomes `notification_receiver.worker_blocked`, makes readiness
fail, and prevents the same in-memory runtime from restarting. `close()` first
signals and joins the worker and closes SQLite only after the callback returns.
If the configured join timeout expires, close fails while durable state remains
open; the supervisor must resolve or terminate the stuck callback before trying
again. Construct the hosted runtime after every process fork.

#### Terminal-receiver status, health, readiness, and capabilities

The durable runtime also serves four authenticated, content-free control routes
on the same exact HTTPS origin:

| Method | Path | Required scope | Purpose |
| --- | --- | --- | --- |
| `POST` | `/v1/localization/terminal-notifications/status` | `terminal-notification-status:read` | Read one site's durable processing state |
| `GET` | `/v1/localization/terminal-notifications/health` | `terminal-notification-health:read` | Inspect aggregate runtime, worker, and durable inbox health |
| `GET` | `/v1/localization/terminal-notifications/readiness` | `terminal-notification-readiness:read` | Check the managed worker and verified inbox |
| `GET` | `/v1/localization/terminal-notifications/capabilities` | `terminal-notification-capabilities:read` | Discover the exact active receiver contract |

Status accepts only canonical UTF-8 JSON and requires
`X-Localization-Terminal-Status-SHA256` to equal the exact body hash:

```json
{"event_id":"cms-event-184","schema":"blun.cms-terminal-receiver-status-request.v1","site_id":"public-site"}
```

Authentication receives the method, origin, path, event, site, and exact body
hash. The returned principal must use
`blun.cms-source-terminal-notification-principal.v1`, the dedicated status
scope, and the same `site_id`. A valid response uses
`blun.cms-terminal-receiver-status-response.v1` and contains only immutable
notification bindings plus processing status, attempt limits, lease timing,
stable error code, and completion time. It never includes the stored payload or
website text. A missing event and another site's event return the same
content-free `404` response.

Readiness is strictly `GET`, body-free, query-free, and separately scoped. Its
`blun.cms-terminal-receiver-readiness.v1` response contains only `status`,
`worker_state`, `inbox_status`, and `error_code`. HTTP `200` requires a running
managed worker and verified `ok` inbox; all other states return `503`. None
of these control routes claims a lease, invokes the host handler, changes retry
state, or performs a model call.

Health is also strictly `GET`, body-free, query-free, and separately scoped.
Its `blun.cms-terminal-receiver-health.v1` response combines `runtime_state`,
`worker_state`, verified `inbox_status`, received and per-state processing
counts, due work, expired leases, terminal failures, and one stable error code.
HTTP `200` requires an open runtime, a running or deliberately unmanaged worker,
and an `ok` inbox. A stopped or failed managed worker, failed processing record,
expired lease, unreadable health state, or damaged inbox returns content-free
`503`. Unavailable counts are `null`; the receiver never invents a healthy
snapshot from partial evidence. Authentication completes before SQLite health
inspection, and the read does not claim, retry, complete, or otherwise mutate
processing state.

Discovery is also strictly `GET`, body-free, query-free, and separately scoped.
Its `blun.cms-terminal-receiver-capabilities-response.v1` envelope contains a
`blun.cms-terminal-receiver-capabilities.v1` contract and canonical SHA-256.
The digest covers all active operations, including the runtime's configured
notification intake path, plus methods, scopes, request and response schemas,
required fields, success statuses, transport limits, processing states, and
terminal outcomes. It contains no site, endpoint origin, notification,
credential, website text, provider response, or private error detail. The
contract includes the health operation's exact method, path, distinct scope,
schema, fields, and success status.

The capability request authenticates the exact empty-body hash before the
contract is built. It never reads the inbox, checks worker readiness, claims a
lease, or calls a handler. A custom intake path that collides with any control
route is rejected before SQLite is created. Missing or altered notification
schema fields, reused scopes, a request body, content type, query, wrong method,
or private authenticator failure returns a content-free fail-closed response.

#### Contract-pinned operator client

`integrations/website_localization_cms_terminal_receiver_client.py` provides a
provider-neutral HTTPS client for the complete receiver contract. Construct
`HTTPTerminalReceiverClient` with the receiver's exact origin, an
`expected_capabilities_sha256` obtained through trusted deployment
configuration, and a callback that supplies authentication headers for the
provided canonical request context. Do not learn and trust the digest from the
same untrusted connection that it is intended to authenticate.

`capabilities()` verifies the response envelope, the canonical digest, every
schema and limit, all five operation definitions, distinct scopes, and the
configured notification path. `health()`, `readiness()`, and `status()` first
repeat that live contract verification; a contract change therefore blocks the
operational read until the deployment deliberately updates its pin. Every HTTP
request is separately authenticated and uses exactly one bounded transport
attempt with no redirects.

`notify()` validates the complete immutable terminal notification before any
network call, repeats the pinned discovery, and sends the canonical bytes to
the notification path taken from that verified live contract. The client is
callable, so the same instance can be passed directly as `terminal_notifier` to
`open_durable_cms_source` or `open_hosted_cms_source`; no separately configured
write adapter or path is required. Its authentication context binds the exact
method, origin, verified path, body SHA-256, notification ID, event, and site.
The client owns no retry loop: `TerminalReceiverClientBlocked` advertises
`cms_notification_failure` plus stable retryability, allowing only the durable
source notification outbox to schedule another attempt. A malformed payload or
capability mismatch blocks before the write request, while an acknowledgement
must exactly match its notification, event, site, acceptance state, and payload
hash.

Health and readiness accept both their documented `200` and `503` states, then
validate exact fields and cross-field invariants before returning the
content-free snapshot. Status binds the canonical request body and its SHA-256
to the event and site, then rejects a response for any other tenant or identity.
All three operational responses also contain `capabilities_sha256`, derived
from the receiver's live configured contract. The client requires this value to
equal its trusted pin after discovery, preventing an endpoint switch or stale
response between the two requests from passing as current evidence.
Malformed JSON, duplicate keys, unexpected fields, inconsistent counts,
rehashed semantic contract drift, redirects, and private transport failures
raise `TerminalReceiverClientBlocked` with only a stable code and retryability.
The three read methods never claim work, mutate receiver state, process a
notification, or contain website text. `notify()` performs only the documented
durable intake operation and returns its content-free acknowledgement.

For a single-process WSGI deployment, `open_hosted_cms_source` adds the owned
worker lifecycle. It starts one non-daemon background worker before returning,
uses interruptible state-specific waits, and joins that worker before closing
any database. `start_worker` and `stop_worker` remain available to supervisors
that need an explicit startup boundary. Worker exceptions become a stable
content-free failure state; the same in-memory runtime cannot restart after
such a failure and must be replaced from its durable databases.

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
| `POST` | `/v1/localization/source/status` | `source-status:read` | Read one site-bound event lifecycle |
| `GET` | `/v1/localization/source/health` | `source-health:read` | Read aggregate content-free health |
| `GET` | `/v1/localization/source/readiness` | `source-readiness:read` | Verify that the managed worker and durable service can accept work |
| `GET` | `/v1/localization/source/capabilities` | `source-capabilities:read` | Discover the exact active HTTP contract |

Authenticated startup requires `capability_preflight=True`; omission blocks
before any database is created. The capabilities route accepts no body or query. Its
`blun.cms-source-capabilities-response.v1` response contains one
`blun.cms-source-runtime-capabilities.v1` contract with the exact active
methods, paths, scopes, principal schemas, request and response schemas,
required top-level fields, success statuses, retry limits, and transport
bounds. The nested `sha256` is calculated over the canonical capability object
before that digest field is added. Clients can pin it during deployment and
reject unexpected contract drift without receiving website content or reading
the three runtime databases. The route has its own
`source-capabilities:read` credential, performs no state write, lease, repair,
or network call, and returns fail-closed if its route metadata is incomplete or
internally inconsistent. The response also carries the runtime's separately
validated `blun.cms-source-capability-binding.v2`: the exact commercial
capability digest, rendering-registry digest, and three durable database roles.

The body-free readiness route uses
`blun.cms-source-readiness-response.v3`. It returns HTTP `200` only while the
managed worker is running and durable service health is `ok` or `degraded`;
startup, shutdown, a worker exception, closed state, or blocked durable health
returns HTTP `503`. Its separate `source-readiness:read` credential receives no
website content. After a runtime enters managed mode, change and removal routes
also require this worker state before persisting new work. Existing manually
driven runtimes retain their explicit `run_once` contract.

Change requests use
`blun.cms-source-change-enqueue-request.v1`; removal requests use
`blun.cms-source-removal-enqueue-request.v1`. Both contain the exact downstream
payload plus an explicit `max_attempts` from 1 through 20. Successful V3
responses return HTTP `202` with the canonical request identity, payload
SHA-256, durable state, attempt count, retry ceiling, exact HTTP capability
digest, and verified runtime binding. Replaying identical bytes converges on
the same durable item. Reusing
an identity with different content or policy returns HTTP `409` and does not
alter stored work.

The status request is exact, query-free JSON and uses
`blun.cms-source-status-request.v1`:

```json
{
  "schema": "blun.cms-source-status-request.v1",
  "event_id": "cms-event-184",
  "site_id": "public-site"
}
```

Its `blun.cms-source-status-response.v5` response contains one nested
`blun.cms-source-service-status.v3` snapshot. It binds the stored
`website_version`, `source_sequence`, canonical change hash, dispatch state and
attempts, remote plan and job count, local lifecycle state, remote lifecycle
status, lifecycle hash, required and approved locales, blocked locale reason
codes, and queue counts. It never contains source text, target text, delivery
payloads, credentials, signatures, provider bodies, or private exceptions.
The read performs no reconciliation, lease, retry, network call, or state
write. A caller can therefore distinguish queued work, a pending local
registration, active localization, approval, publication, cancellation, and a
terminal failure without accidentally advancing the worker.

Version 2 additionally exposes only the terminal notification state, its
content-free identity and hash, bounded attempt counters, and a public error
code. When the terminal notifier exposes the pinned receiver `status` method,
the snapshot also reports the independently durable processing observation,
poll failures, receiver attempts, and stable local and receiver error codes.
An intake acknowledgement is never presented as completed CMS processing.

The corresponding `blun.cms-source-health-response.v5` and nested
`blun.cms-source-service-health.v3` report notification and processing-observer
backlog plus component
health without revealing website content or callback responses.

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

For change, removal, health, and capabilities routes, the authenticator returns
exactly
`blun.cms-source-runtime-principal.v1` with `principal_id`, `credential_id`,
`credential_version`, and the one route-specific `scope`. Status uses the
separate `blun.cms-source-status-principal.v1` schema and additionally binds
one exact `site_id`. The request site must match that principal before SQLite
is queried; another site and an unknown event both return the same HTTP `404`
error. The runtime does not interpret credentials and never stores them.
Operators should give status, health, and write routes distinct credentials.
The WSGI server remains responsible for rejecting ambiguous wire-level HTTP
before constructing the WSGI environment.

Requests require an exact query-free HTTPS route, fixed `Content-Length`, UTF-8
JSON, and no transfer encoding. Health and capabilities accept no body or
content type. Every response sets `Cache-Control: no-store`,
`X-Content-Type-Options: nosniff`, and `Referrer-Policy: no-referrer`. Errors use
`blun.cms-source-runtime-http-error.v1` and never echo a body, header, path,
credential, exception, source string, or target string.

#### Contract-pinned website client

`integrations/website_localization_cms_source_client.py` is the provider-neutral
client for this complete ingress. Construct `CMSLocalizationSourceHTTPClient`
with one exact HTTPS origin, the HTTP contract pin, and the source runtime's
commercial capability and rendering-registry pins supplied through trusted
deployment configuration. A callback provides authentication headers for the
canonical request context and receives the method,
origin, verified path and scope, body SHA-256, and the applicable event, site,
or request identity; it cannot replace framing, idempotency, or binding headers.

`submit_change()` and `submit_removal()` validate the complete V2 CMS change or
V1 cancellation/tombstone locally before discovery and before any write. Each
method fetches the capability object, requires its canonical digest and every
operation definition to match the installed contract and trusted pin, takes
the write path from that fresh result, and performs exactly one request. The
immutable event, cancellation, or tombstone identity is the idempotency key.
An accepted response must return the same operation, request and event IDs,
canonical inner-payload hash, retry ceiling, capability digest, and exact
runtime binding.

`status()`, `health()`, and `readiness()` repeat discovery independently and
then validate every returned field and cross-field invariant. Status is bound
to the requested event and site. Health and readiness accept their documented
`200` and `503` states only when the HTTP status agrees with the nested state.
All five operational response schemas carry `capabilities_sha256` and the
runtime binding. A missing, malformed, substituted, or stale binding blocks
even when the static HTTP schema still matches its separate contract pin.

The adapter owns no retry loop. `CMSSourceClientBlocked` contains only a stable
content-free code and retryability decision, so the website host may apply its
own bounded retry policy. Network failures, `408`, `425`, `429`, and server
errors are retryable; redirects, contract drift, malformed JSON, unexpected
fields, cross-tenant evidence, payload mismatches, and invalid requests are
not. The client never returns website text through a status or health method
and never treats a failed or ambiguous response as accepted work.

#### Durable website-source delivery

`integrations/website_localization_cms_source_delivery.py` supplies the durable
retry policy for website and CMS hosts. Construct
`DurableCMSSourceDeliveryOutbox` with a caller-owned SQLite connection and one
already configured `CMSLocalizationSourceHTTPClient`. Enqueue the complete
change with `enqueue_change()` or a cancellation/tombstone with
`enqueue_removal()` before allowing the originating transaction to be treated
as handed off. Each stored item binds its canonical payload SHA-256, immutable
request and event IDs, site, source-service retry ceiling, delivery retry
ceiling, and the client's trusted capability digest.

`run_once()` claims at most one due item under an unpredictable token-bound
lease and performs exactly one client operation. Removal work is selected
before change work. A retryable client failure schedules durable exponential
backoff up to the delivery ceiling; a permanent failure or exhausted ceiling
becomes terminal. The independent `source_max_attempts` value is sent to the
remote source service and is never multiplied into the local delivery limit.

If the website process exits after remote acceptance but before the local
success commit, lease expiry makes the same canonical payload and idempotency
identity eligible again. The source service decides that replay idempotently.
A stale worker cannot complete or fail a lease after another worker has taken
it over. Choose `lease_seconds` greater than the configured client timeout,
run one tick at a time from a supervised worker, and keep the SQLite database
on durable private storage.

`status()` returns one content-free binding and attempt snapshot. `health()`
reports aggregate queue states, due work, expired leases, terminal failures,
and active rows pinned to another capability digest. SQLite schema or row
tampering blocks before client access. Contract drift does not silently move
queued writes to a newly advertised endpoint: the deployment must explicitly
resolve or migrate those immutable records first.

#### Hosted website-source delivery runtime

`integrations/website_localization_cms_source_delivery_runtime.py` is the
production composition root for that outbox. Call
`open_durable_cms_source_delivery()` with one absolute private database path,
one pinned source client, and a stable worker ID. It validates the client,
worker ID, timeout, lease, and backoff policy against an in-memory outbox before
creating the file. The resulting runtime owns its SQLite connection and must be
constructed after a prefork server creates the final worker process.

The database file is created mode `0600` without following links. Its complete
parent chain, owner, type, link count, permissions, device, and inode are
checked before and after every operation. A missing, replaced, linked, or
permission-weakened file blocks before client access. One reentrant runtime
lock serializes callers inside a process, while SQLite transactions and durable
leases let separately constructed processes share the same database safely.
An inherited runtime rejects the foreign process before attempting its lock.

`open_hosted_cms_source_delivery()` additionally starts one process-owned,
non-daemon background worker. It uses interruptible active, idle, and blocked
waits, claims one network attempt per tick, and gives removal events the same
priority defined by the outbox. Once managed, `enqueue_change()` and
`enqueue_removal()` accept new work only while that worker is alive and has no
recorded failure. Manual runtimes remain available for an external supervisor
that calls `run_once()` itself.

`worker_readiness()` returns only worker state, outbox health state, and a
stable error code. It is ready only when the managed worker is alive and the
outbox reports `ok`. `stop_worker()` signals before waiting, so an idle worker
wakes immediately. If a source-client call exceeds the configured join bound,
shutdown returns `source_delivery_runtime.worker_stop_timeout` and deliberately
keeps the database connection open; close it only after the worker finishes.
Worker exceptions are reduced to `source_delivery_runtime.worker_blocked`, and
the failed runtime cannot accept more managed source events.

When the source client supplies the verified runtime-capability and commercial
rendering-registry pins, the durable outbox also creates one canonical
`source_delivery` binding record. It binds those two generations together with
the source-delivery contract hash and validates the table shape, exact row, and
derived digest before every queue operation. A same-generation restart resumes
pending work. Only an empty unbound legacy database may be bound automatically;
a non-empty legacy queue, changed generation, altered metadata, or unpinned
reopen of an already bound database blocks before queue or network access. The
production HMAC website-to-sidecar composition binds its separate outer database
to the same verified runtime and rendering generations. On restart it checks the
existing file and canonical binding read-only before any capability request. A
local schema, file-safety, or generation failure therefore makes no network
request; a new database or empty unbound legacy database proceeds to the
authenticated downstream preflight before creation or migration.

#### Website-source delivery HTTP sidecar

`integrations/website_localization_cms_source_delivery_http.py` makes the
protected website outbox available to CMS and website backends that do not
embed Python. Pass `http_authenticator` to
`open_hosted_cms_source_delivery()` and serve the resulting `runtime.http`
WSGI application behind TLS. Invalid authentication configuration is rejected
during in-memory preflight before the SQLite file is created. Omitting the
option preserves the manual Python runtime and leaves `runtime.http` as `None`.

The sidecar exposes these independently authorized operations:

- `POST /v1/localization/source-delivery/changes`
- `POST /v1/localization/source-delivery/removals`
- `POST /v1/localization/source-delivery/status`
- `POST /v1/localization/source-delivery/source-status`
- `GET /v1/localization/source-delivery/health`
- `GET /v1/localization/source-delivery/readiness`
- `GET /v1/localization/source-delivery/source-health`
- `GET /v1/localization/source-delivery/source-readiness`
- `GET /v1/localization/source-delivery/capabilities`

The authenticator receives schema
`blun.cms-source-delivery-sidecar-auth-request.v1` with the exact method, path,
sorted request headers, and SHA-256 of the received body. It returns a stable
principal, credential identity and version, and the route's exact scope.
Change, removal, status, and source-status routes additionally require the
authorized `site_id`; the sidecar compares it independently with the request
and the runtime result. An unknown request and one belonging to another
website both return the same content-free `404` response.

Change and removal bodies carry the complete immutable payload plus distinct
`source_max_attempts` and `delivery_max_attempts` values. The
`Idempotency-Key` must equal the event, cancellation, or tombstone ID, and
`X-Localization-Source-Payload-SHA256` must equal the canonical inner-payload
hash. HTTP `202` is returned only after the exact binding has been persisted.
The sidecar performs no delivery attempt itself; the managed worker retains
the single-attempt, durable-backoff, and crash-recovery semantics.

Status, lifecycle, health, and readiness operations never return website text.
The source-status route is gated on an exact durably accepted sidecar row; the
body-free source-health and source-readiness routes keep downstream queue state
and processing availability separate from sidecar intake. Every outgoing
runtime object is checked for its exact field set, types, hashes, state
invariants, request
identity, and applicable tenant before serialization. The capability route
describes all nine schemas, methods, paths, scopes, limits, and safety
semantics under one canonical SHA-256. That digest is repeated on every
operational response, and any internal contract drift blocks the complete
response instead of advertising a rehashed weakened interface. Each of the
three source-facing reads also carries the separately validated source runtime
binding without exposing content.

#### Contract-pinned source-delivery sidecar client

`integrations/website_localization_cms_source_delivery_client.py` is the
provider-neutral HTTPS reference client for all nine sidecar operations.
Construct `CMSSourceDeliverySidecarHTTPClient` with one exact HTTPS origin, the
trusted sidecar capability SHA-256, the separately trusted downstream
source-service capability SHA-256, and a callback that returns authentication
headers for the immutable request context. Loopback HTTP is available only
through the explicit test/development option.

Before every operational request the client fetches the live capability
object, validates its complete canonical form against the installed contract,
and requires its digest to equal the deployment pin. The subsequent request
uses only the method, path, schema, and success status from that fresh object.
The authentication callback receives the origin, method, verified path, scope,
exact body SHA-256, and applicable site, event, request, and payload identities
under `blun.cms-source-delivery-sidecar-client-auth-context.v1`.
It cannot supply `Host`, framing, content type, idempotency, or source-payload
binding headers.

`submit_change()` and `submit_removal()` validate and canonically copy the
complete source event before discovery. The body carries distinct source and
delivery retry ceilings, while the reserved payload header hashes only the
immutable inner event. One call performs one transport attempt and follows no
redirect. Acceptance requires the exact request, event, tenant, payload,
retry-policy, sidecar-contract, and downstream-contract bindings.

`status()` requires the caller's already known operation, request, event,
tenant, and payload hash; a response cannot silently substitute another
durable item. `source_status()` adds the complete validated localization
lifecycle and source runtime binding only after exact durable source
acceptance. `health()`, `readiness()`,
`source_health()`, and `source_readiness()` accept HTTP `503` only as an exactly
validated blocked snapshot. Transport and server failures expose stable
content-free codes plus retryability, but the client never schedules a retry.

For the authenticated production path, construct
`RotatingHMACCMSSourceDeliveryClient` from
`website_localization_cms_source_delivery_auth_runtime.py`. Supply the exact
origin, sidecar capability SHA-256, downstream capability SHA-256, initial
`HMACCredential`, clock/nonce policy, timeout, and optional transport once. The
composition creates one private `RotatingSourceDeliveryHMACSigner` and one
`CMSSourceDeliverySidecarHTTPClient` from that same immutable configuration.
Invalid construction is reduced to
`source_delivery_hmac.client_configuration_invalid` before any network call.

The composed surface exposes `capabilities()`, `submit_change()`,
`submit_removal()`, `status()`, `source_status()`, `health()`, `readiness()`,
`source_health()`, and `source_readiness()` with the original contract
signatures. `replace_credential()` updates the exact signer used by all nine
routes. The client is process-bound before delegation, so a forked worker
cannot reach its inherited transport; create a fresh client in the child from
host-owned secret state. Client errors retain the underlying stable
content-free retry decision,
and neither the wrapper nor its representation exposes the credential, tenant,
endpoint, or website content.

#### Owned authenticated website submission runtime

`integrations/website_localization_cms_source_delivery_submission_runtime.py`
is the production composition root for a website process that submits through
the authenticated sidecar. Use
`open_durable_hmac_cms_source_delivery_submission()` for an externally driven
loop or `open_hosted_hmac_cms_source_delivery_submission()` to start the owned
non-daemon worker. Both construct one `RotatingHMACCMSSourceDeliveryClient`,
one `CMSSourceDeliverySidecarOutboxAdapter`, and one guarded SQLite runtime.

Supply the initial `HMACCredential`, exact HTTPS origin, trusted sidecar and
downstream capability hashes, the expected source-runtime capability SHA-256,
the expected commercial rendering-registry SHA-256, worker identity, and the
middle `sidecar_delivery_max_attempts` once. Before opening the website SQLite
file, the composition reads source readiness through the authenticated sidecar
and requires its verified capability binding to match both generation pins.
Unavailable, missing, partial, malformed, or substituted evidence blocks with
a stable content-free error before database creation. The hosted factory still
validates all loop delays before that preflight.

Both verified generation values become part of the adapter capability digest
and the outer outbox's role-specific durable binding. A restart therefore
resumes pending website work only under the exact same sidecar adapter,
source-runtime, and commercial rendering generation. The SQLite file retains
the existing owner-only, process-bound, inode-guarded lifecycle.

Call `submission_capabilities()` to discover the exact live contract of this
complete website edge. The content-free
`blun.cms-source-delivery-submission-capabilities.v1` snapshot advertises the
accepted change and removal schemas, all six composed operational projection
schemas, the separately owned website, sidecar and source retry budgets, and
the explicit rule that durable source acceptance is not publication. It also
states that this edge neither generates translations nor grants publication
authority. Its
canonical SHA-256 binds the locally verified website generation to the current
sidecar and source-service capability pins.

The method validates the guarded SQLite generation before it performs one
fresh authenticated sidecar capability request. A missing or changed local
binding therefore causes no network traffic. A stale, substituted or malformed
sidecar contract blocks the whole snapshot; the runtime never returns partial
capabilities. Returned nested maps are defensive copies and contain no
endpoint, credential, tenant, source text, target text or project price.

`enqueue_change()` and `enqueue_removal()` persist work before transport.
Their `delivery_max_attempts` controls only website-to-sidecar acceptance;
their `source_max_attempts` remains the final processing ceiling; the factory's
middle limit controls sidecar delivery. `status()` and `health()` describe the
local acceptance outbox. `sidecar_status()`, `sidecar_health()`,
`sidecar_readiness()`, `sidecar_source_status()`,
`sidecar_source_health()`, `sidecar_source_readiness()`, and
`sidecar_capabilities()` perform separately authenticated operational reads and
never reinterpret local success as downstream completion.

Use `submission_status(operation, request_id)` for a single content-free
projection across both acceptance queues. While the website row is pending,
leased, retrying, or failed, it performs no network request and reports stage
`website_acceptance`. Only after local success does it query the sidecar with
the persisted operation, request, event, site, and payload hash. The response
must preserve those bindings, both capability pins, the configured middle
delivery ceiling, and the source-processing ceiling.

The projection schema is
`blun.cms-source-delivery-submission-status.v2`. Its top-level status is
`pending`, `failed`, or `accepted`; its stage is `website_acceptance`,
`sidecar_delivery`, or `source_acceptance`. It includes the separate website
and sidecar states, attempt counts and retry ceilings, the next-attempt time,
lease-expiry flag, and a stable content-free error code. `accepted` means the
source service has durably accepted the event. It does not mean translation,
quality review, release approval, or publication succeeded.

The `website_capability_binding` field is independently recomputed from the
validated, role-specific SQLite generation before the status is projected. It
contains only the outer adapter capability hash, source-runtime hash,
commercial rendering-registry hash, database role, and their canonical binding
hash. A missing, changed, or malformed binding blocks locally before a sidecar
status request.

`submission_lifecycle()` extends that accepted state with the independently
validated source lifecycle. Its
`blun.cms-source-delivery-submission-lifecycle.v3` projection keeps submission,
source status, the verified website generation, and the verified source runtime
binding separate. Missing or changed binding evidence blocks instead of
returning a processing state.

Use `submission_readiness()` to inspect the complete durable intake path
without collapsing its two independently operated workers. The method first
validates the website worker's local readiness object. If that worker or its
outbox is not ready, it returns `not_ready` without making a network request.
Only a locally ready runtime performs the authenticated, contract-pinned
sidecar readiness request.

The content-free
`blun.cms-source-delivery-submission-readiness.v2` projection retains separate
website and sidecar readiness, worker state, outbox state, and stable error
code fields. It also carries the trusted sidecar and source-service capability
hashes plus the locally verified website generation. Overall status is `ready`
only when both workers report `running`, both
outboxes report `ok`, both component error codes are absent, and the sidecar
response matches the currently pinned contract. This is intake readiness; it
does not assert that a particular localization or publication has completed.

Use `submission_pipeline_readiness()` when the website must also prove that the
source localization service can process accepted work. The method evaluates
the existing intake projection first. If the website worker or sidecar is not
ready, `source_readiness` remains `null` and no request reaches the next layer.
Only fully ready intake performs the separately authenticated, body-free
`source_readiness()` operation through the sidecar.

The resulting
`blun.cms-source-delivery-submission-pipeline-readiness.v3` object keeps the
complete intake projection and source-worker projection separate, binds the
current sidecar and source-service capability hashes plus the website and
source runtime bindings, and reports overall
`ready` only when both projections are independently ready. A stopped source
worker, transport failure, malformed status combination, or capability drift
blocks fail-closed. This operational probe is content-free and makes no claim
that any particular locale has passed review or publication.

Use `submission_health()` for one content-free operational view of both
durable acceptance outboxes. The runtime validates its local health object
before making any network request, then retrieves the sidecar health through
the owned authenticated, contract-pinned client. Invalid local state therefore
blocks offline; malformed remote counters, contradictory status, and changed
capability bindings also block fail-closed.

The `blun.cms-source-delivery-submission-health.v2` projection retains the
complete whitelisted website and sidecar health snapshots separately,
including counts, operation totals, due work, expired leases, terminal
failures, contract mismatches, and stable error codes. Its overall status is
`ok` only when both snapshots independently report `ok`. A blocked component
can never be hidden by the other component's healthy state. The exact website
generation binding is checked locally and included before the sidecar health
request is allowed.

Use `submission_pipeline_health()` to extend that health view through every
durable source-processing queue. The runtime evaluates the website and sidecar
intake projection first. If either intake outbox is blocked, `source_health`
remains `null` and no source-health request is made. Healthy intake performs a
separately authenticated, body-free source-health request through the sidecar.

The resulting `blun.cms-source-delivery-submission-pipeline-health.v3` object
keeps the intake projection and complete source-service projection separate,
binds both current capability hashes and the website and source runtime
bindings, and
preserves `ok`, `degraded`, or
`blocked` source state. Invalid counters, contradictory HTTP status, transport
failure, or capability drift block fail-closed without returning content.

Call `replace_credential()` only during a server-side generation overlap. It
updates the exact signer owned by the worker without reopening the outbox or
changing either contract pin. The wrapper blocks network and storage access
after close or from a forked process; each child must create a fresh runtime
from host-owned configuration. Representations and stable failures contain no
secret, endpoint, tenant, source text, or provider response.

#### Durable website submission through the sidecar

`integrations/website_localization_cms_source_delivery_sidecar_adapter.py`
connects that sidecar client to `DurableCMSSourceDeliveryOutbox` without
collapsing retry ownership. Wrap the pinned or rotating-HMAC sidecar client in
`CMSSourceDeliverySidecarOutboxAdapter`, configure the sidecar's downstream
`sidecar_delivery_max_attempts`, and pass the adapter to
`open_durable_cms_source_delivery()` or
`open_hosted_cms_source_delivery()`.

The runtime's `delivery_max_attempts` remains the website's ceiling for
obtaining durable sidecar acceptance. `source_max_attempts` remains the source
service's processing ceiling. The adapter's fixed
`sidecar_delivery_max_attempts` is the independent middle ceiling used after
acceptance. A transient failure reaching the sidecar therefore consumes only
the outer budget.

The adapter derives the outbox capability binding from a canonical record of
the sidecar capability pin, downstream source-service capability pin, and
middle retry ceiling. Any change blocks existing active rows before network
access. Only an exact sidecar acceptance envelope is projected into the
outbox's private completion receipt; downstream status is never represented as
completed and must still be read through the sidecar status/lifecycle APIs.
Malformed acknowledgements and undeclared exceptions fail closed.

#### Rotatable source-delivery HMAC authentication

`integrations/website_localization_cms_source_delivery_auth.py` provides a
complete provider-neutral authentication implementation for the client and
sidecar callback contracts. It is optional: deployments may still use bearer
tokens, mutual TLS, an external identity proxy, or another verifier. HMAC here
authenticates transport requests; it is not a linguistic-quality signature and
cannot replace either review stage or a signed publication approval.

Create an `HMACCredential` from a host-owned secret of at least 32 bytes, an
explicit credential ID and generation, a sorted allowlist of route scopes, and
the one authorized `site_id` whenever tenant scopes are present. The credential
object redacts the secret from representations. Secrets are passed in memory;
the module never reads, writes, generates, or rotates a live key file.

`SourceDeliveryHMACSigner` is the `authentication_headers` callback for
`CMSSourceDeliverySidecarHTTPClient`. `SourceDeliveryHMACVerifier` is the
matching `http_authenticator` callback for
`open_hosted_cms_source_delivery()`. Configure both from trusted deployment
state with the identical HTTPS origin, sidecar capability SHA-256, downstream
source-service capability SHA-256, and accepted credential generation. The
canonical proof binds those values together with the exact method, live
contract path, route scope, body hash, site, event, request and payload
identities, and the actual idempotency and source-payload headers.

For a long-lived client, use `RotatingSourceDeliveryHMACSigner` directly as
the `authentication_headers` callback. Its origin, sidecar capability digest,
downstream capability digest, clock policy, nonce source, and transport policy
are fixed when it is constructed. `replace_credential()` validates one entire
new `HMACCredential` and atomically replaces only the credential-bound signer
under the same lock used to create proofs. Invalid replacement state keeps the
last valid signer. The wrapper is process-bound and rejects inherited use after
fork; each child must obtain its own host-supplied credential and signer.

Every proof has a short bounded timestamp and a random nonce. The verifier
checks the HMAC with constant-time comparison and then calls
`DurableHMACReplayStore.consume()` before returning a principal. Construct the
store over a dedicated caller-owned SQLite connection; threaded WSGI hosts
must open that connection with `check_same_thread=False`. Keep its database on
the same class of private durable storage as the delivery outbox and close it
only after the HTTP host has stopped. The ledger contains only credential IDs,
versions, nonces, proof hashes, and validity times—never secrets, request
bodies, website text, translations, or provider responses.

The store validates its schema and every retained row inside the same immediate
transaction that consumes a nonce. Exact replay returns an invalid principal
and therefore HTTP `401`. A store transaction conflict, malformed retained row,
SQLite outage, or invalid clock raises a stable content-free infrastructure
failure, which the sidecar maps to retryable HTTP `503` without accepting the
request. Multiple credential generations may be configured simultaneously for
a bounded rotation window; removing a generation retires it immediately.

#### Protected authenticated sidecar runtime

`integrations/website_localization_cms_source_delivery_auth_runtime.py`
provides the production composition for the V6.67 proof. Call
`open_hosted_hmac_authenticated_cms_source_delivery()` with distinct absolute
outbox and replay-database paths, the provider-neutral source client, one or
more explicit `HMACCredential` generations, worker identity, fixed HTTPS
origin, and both trusted capability digests. The returned object owns the
supervised delivery worker, sidecar WSGI application, outbox connection,
authentication verifier, and replay connection as one lifecycle.

Import the server-side `HMACCredential` alias from this runtime module so the
credential values and verifier share the exact validated contract type. The
secret still enters only as host-owned in-memory bytes and is never written to
either database.

The factory validates the complete authentication time window, credential
scope, endpoint, contract pins, worker, retry policy, loop delays, SQLite
timeouts, and distinct paths in memory before it opens either database. Each
missing database is created exclusively with mode `0600`. Every later proof or
authentication-health read rechecks the replay file's owner, type, link count,
mode, device and inode, plus its safe parent chain. The runtime checks its
creating process before entering a lock, serializes all connection use, and
therefore rejects inherited pre-fork instances before SQLite or protected
website state is touched.

`authentication_health()` returns only schema, status, runtime state,
consumed-nonce count, and stable error code. A local failure produces a
`blocked` snapshot with no count. It never includes a path, site, credential, key, request
body, source text, target text, or provider output. A missing, replaced,
linked, permission-weakened, corrupt, closed, or foreign-process replay store
makes the sidecar return retryable HTTP `503` during authentication, before
the delivery runtime parses or persists protected content.

Use `replace_credentials()` on either the composite runtime or its
`authentication` member to rotate without stopping the delivery worker. A
safe rollout proceeds in this order:

1. Supply the old and new server generations together.
2. Call `replace_credential()` on every client instance and verify traffic.
3. Drain requests already emitted with the old credential and wait out the
   configured proof validity and clock-skew window, unless an equivalent
   deployment traffic barrier proves that none remain.
4. Supply only the new server generation.

The client signer lock serializes proof creation in one process. It cannot
recall a proof already returned to the HTTP client, synchronize other fleet
instances, or prove that a request has left the network. The server must
therefore keep the old generation during the bounded drain window. The
method materializes and validates the complete iterable, checks the replay
database and process lifecycle, waits behind any in-flight authentication,
and swaps exactly one fully constructed verifier. Invalid or unavailable
replacement state leaves the previous verifier untouched. The same replay
ledger remains open throughout, so a consumed proof stays consumed even if
its credential generation is removed and later accepted again.

The host remains responsible for fetching trusted secret-manager values and
for deciding the overlap window. The runtime does not log, persist, return, or
rotate secret material, and it never reads a key file or environment variable.

Call `close()` only after the external HTTP server has stopped accepting new
requests. The composite first signals and joins the delivery worker and closes
the outbox, then closes the replay connection. If a provider call exceeds the
configured worker-stop bound, the replay runtime deliberately remains open;
the caller must resolve the still-running worker and retry shutdown rather
than invalidating authentication underneath it.

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

The nested `blun.website-localization-capabilities.v5` object carries a
`sha256` value over all its other canonical fields. Consumers can pin that
digest for a deployment and deliberately reconfigure when it changes. The
runtime rebuilds and validates the complete registry on every read; duplicate,
missing, noncanonical, or profile-mismatched entries return a fail-closed `503`
without a partial locale list.

Within it, `commercial_profile` is a separately hashed
`translate-native.commercial-capabilities.v5` object. Its nested and separately
hashed `review_summary_contract` defines the exact content-free result schema,
field set, verified/review-required state invariant, ten allowed ordered
review dimensions, exact source/target/profile/evidence hash semantics, and
excluded sensitive content. A CMS or independent-review adapter can validate
targeted commercial
escalation without receiving project prices, brands, source/target text, spans,
or reviewer prose. Any registry or digest drift blocks the whole discovery
response rather than advertising a partial contract.

The commercial capability additionally requires schema
`translate-native.commercial-locale-quality-profile.v2` in every commercial
job. One distinct version and digest is derived for each advertised EU locale
from that locale's native, fidelity, adversarial and source-reference profile.
The full object is bound into the job ID and all three provider requests; its
version and digest remain in signed quality evidence. A stale, substituted or
mutated locale profile blocks before any provider call.

The profile includes a canonical
`translate-native.commercial-rendering-reference.v1` derived from the tagged
Unicode CLDR 48 numbers data for that exact target profile. Its default and
native numbering systems, grouping threshold, symbols, and decimal, percentage,
currency, ISO-currency, approximation, limit, and range patterns are sent to
all three provider phases and covered by the profile digest. This is rendering
guidance only: deterministic punctuation or numeric regex matching is not
semantic proof, equivalent written forms remain eligible, and uncertainty is
routed to independent model or qualified native-domain review.

The separately hashed `commercial_rendering_registry` makes those exact
references available to CMS and website clients without exposing localized
content. It contains all 24 locales in canonical order and binds each reference
to the exact commercial locale-profile version and digest advertised in the
same response. The runtime reconstructs and compares the complete registry
before returning capabilities; a missing, reordered, altered, or merely
rehashed entry returns `503` without a partial registry. Consumers must still
treat these values as display guidance and route uncertain semantic equality to
the configured independent review path.

For publication, `blun.website-localization-release-evidence.v3` carries the
compact `commercial_quality_profile` binding `{profile, version, sha256}` for
commercial content and requires all three fields to be null for every other
content type. The reference CMS receiver recomputes the canonical version and
digest for each exact target locale before calling host code. A syntactically
valid digest, a binding from another EU locale, or a prior profile generation
is therefore not accepted merely because the generic commercial profile still
matches. An unresolved commercial summary additionally requires
`commercial_review_resolution` with the exact ordered dimensions, a
`qualified_human` or `independent_model` method, the verified receipt hash, and
the independent provider binding only for the model route. Verified commercial
summaries and non-commercial content require this field to be `null`. Raw
receipts, qualified-human identities and reviewer prose are never published.

```json
{
  "capabilities": {
    "change_schema": "blun.cms-content-change.v2",
    "cancellation_schema": "blun.cms-content-cancellation.v1",
    "commercial_profile": {
      "profile": "translate-native.commercial.v5",
      "locale_quality_profile": {
        "schema": "translate-native.commercial-locale-quality-profile.v2",
        "required": true,
        "binding_fields": ["locale", "version", "commercial_profile", "quality_profile_version", "quality_profile_sha256", "rendering_reference", "sha256"],
        "rendering_reference": {"schema": "translate-native.commercial-rendering-reference.v1", "authority": "Unicode CLDR", "version": "48", "purpose": "target-locale-rendering-guidance", "semantic_proof": false, "unresolved_route": "independent-model-or-qualified-native-domain-review"},
        "required_commercial_checks": ["amount_currency", "discount_basis", "qualifiers", "tax_status", "billing_interval", "commitment", "renewal", "cancellation", "conditions", "offer_assignment"],
        "provider_phases": ["transcreation", "target_native", "source_fidelity"],
        "tamper_policy": "block-before-provider"
      },
      "review_summary_contract": {
        "content_policy": {"project_brands": false, "project_prices": false, "reviewer_prose": false, "source_spans": false, "source_text": false, "target_spans": false, "target_text": false},
        "evidence_sha256": {"algorithm": "sha-256", "binding_fields": ["schema", "profile", "source_sha256", "target_sha256", "evidence"], "binding_schema": "translate-native.commercial-review-evidence-binding.v1", "canonicalization": "utf-8-json-sort-keys-no-insignificant-whitespace", "covers": ["commercial-profile", "exact-source-sha256", "exact-target-sha256", "complete-commercial-review-evidence"], "text_hashing": "exact-utf-8"},
        "profile": "translate-native.commercial.v5",
        "required_fields": ["schema", "profile", "status", "review_required_dimensions", "evidence_sha256"],
        "result_schema": "translate-native.commercial-review-summary.v2",
        "review_required_dimensions": {"allowed": ["amount_currency", "discount_basis", "qualifiers", "tax_status", "billing_interval", "commitment", "renewal", "cancellation", "conditions", "offer_assignment"], "order": ["amount_currency", "discount_basis", "qualifiers", "tax_status", "billing_interval", "commitment", "renewal", "cancellation", "conditions", "offer_assignment"], "unique": true},
        "schema": "translate-native.commercial-review-summary-capabilities.v2",
        "sha256": "<sha256>",
        "statuses": {"review_required": {"requires_independent_review": true, "review_required_dimensions": "one-or-more"}, "verified": {"review_required_dimensions": "empty"}}
      },
      "schema": "translate-native.commercial-capabilities.v5",
      "sha256": "<sha256>"
    },
    "commercial_rendering_registry": {
      "commercial_profile": "translate-native.commercial.v5",
      "content_policy": {"credentials": false, "project_brands": false, "project_prices": false, "source_text": false, "target_text": false},
      "locales": [{
        "commercial_quality_profile": {"sha256": "<sha256>", "version": "commercial-eu-mt-MT-2026-09-2"},
        "locale": "mt-MT",
        "rendering_reference": {
          "locale": "mt-MT",
          "minimum_grouping_digits": 1,
          "native_numbering_system": "latn",
          "numbering_system": "latn",
          "patterns": {"currency": "¤#,##0.00", "decimal": "#,##0.###", "percent": "#,##0%", "range": "{0}–{1}"},
          "schema": "translate-native.commercial-rendering-reference.v1",
          "source": {"authority": "Unicode CLDR", "locale": "mt", "version": "48"},
          "symbols": {"decimal": ".", "group": ","}
        }
      }],
      "schema": "translate-native.commercial-rendering-registry.v1",
      "sha256": "<sha256>",
      "source": {"authority": "Unicode CLDR", "version": "48"}
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
      "commercial_quality_profile_sha256": "<sha256>",
      "commercial_quality_profile_version": "commercial-eu-mt-MT-2026-09-2",
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
      "release_evidence_schema": "blun.website-localization-release-evidence.v3",
      "response_content_types": ["application/json", "application/json; charset=utf-8"],
      "schema": "blun.cms-localization-publication-http-capabilities.v2",
      "sha256": "<sha256>"
    },
    "publication_schema": "blun.cms-localization-publication.v3",
    "quality_passes": ["target_native", "source_fidelity"],
    "schema": "blun.website-localization-capabilities.v5",
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
