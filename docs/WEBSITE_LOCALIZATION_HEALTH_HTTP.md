# Authenticated localization health HTTP contract

`WebsiteLocalizationHealthHTTPApplication` exposes the composed runtime's
existing read-only health monitor to an explicitly authorized operator. It is
provider-neutral and returns identifiers, lifecycle states, stable reasons,
and counts only. It never repairs state, advances a lease, retries work, signs
an approval, calls a model, publishes content, or returns source text, target
text, reviewer prose, credentials, receipts, or transport exceptions.
An explicitly configured publisher probe may make one content-free callback
challenge as part of the read.

This is an operator endpoint, not a tenant CMS status endpoint. Its response
can contain site and website-version identifiers from the complete configured
runtime. Do not grant a normal site credential access. Tenant-scoped progress
continues to use the separately signed endpoint documented in
[`WEBSITE_LOCALIZATION_API.md`](WEBSITE_LOCALIZATION_API.md).

## Runtime configuration

Pass a callable `health_http_authenticator` to
`WebsiteLocalizationRuntime`. The runtime then exposes `runtime.health_http`.
Without that explicit capability the attribute is `None` and no operator
reader exists. Optional `health_provider_probe` and `health_publisher_probe`
values must implement `check` and are accepted only together with the
authenticator. Invalid or partial settings block before any SQLite schema is
created or migrated. The publisher probe must be the exact same capability as
the runtime's delivery publisher. The built-in HTTPS publisher implements it
with a fresh probe ID and the exact advertised callback-contract digest; its signed
response contains no website or locale data.

Mount the WSGI callable behind a production server and trusted TLS terminator.
The application requires `wsgi.url_scheme == "https"`; only the trusted server
may derive that value. It accepts one empty, query-free request:

```http
GET /v2/localization/health HTTP/1.1
Authorization: <operator-owned credential>
Content-Length: 0
```

Bodies, transfer encoding, duplicate or malformed headers, plaintext
transport, queries, other methods, and other paths are rejected before the
authenticator or monitor runs. Request headers are bounded and passed only to
the host authenticator in this canonical envelope:

```json
{
  "schema": "blun.localization-health-http-auth-request.v1",
  "method": "GET",
  "path": "/v2/localization/health",
  "headers": [["authorization", "<operator-owned credential>"]],
  "body_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
}
```

The host owns credential lookup, rotation, rate limiting, network policy, and
audit redaction. On success it returns exactly:

```json
{
  "schema": "blun.localization-health-reader-principal.v1",
  "reader_id": "operations-1",
  "credential_id": "health-reader-1",
  "credential_version": "2026-09-09",
  "scope": "service-health"
}
```

The adapter validates every field and requires the exact `service-health`
scope. It does not include principal or credential values in the response.
Invalid principals return `401`; an authenticator outage returns a retryable
`503`, with no exception text.

## Provider-neutral reference client

`integrations/website_localization_health_client.py` consumes this endpoint
without choosing an identity provider or retry scheduler. The host supplies a
credential-header callback; the client calls it once per read and performs one
bounded request:

```python
import time

from integrations.website_localization_health_client import (
    WebsiteLocalizationHealthClient,
)

client = WebsiteLocalizationHealthClient(
    "https://localization.example",
    lambda: {"Authorization": operator_token()},
    clock=time.time,
)
snapshot = client.read()
```

`snapshot.http_status` is `200` for a valid healthy or degraded assessment and
`503` for a valid blocked assessment. In both cases `snapshot.as_payload()` is
the complete validated content-free report, so callers retain per-component
and per-locale reasons such as `release.policy_unavailable`,
`release.policy_stale`, and `release.integrity_failed`.

The client requires HTTPS except for an explicitly enabled loopback test
origin, refuses redirects, caps one response at four megabytes, checks the
declared byte length and security headers, rejects duplicate JSON keys, and
requires the report timestamp to fall within a configurable age and future
skew. Remote contract errors retain their exact stable code and retry flag;
local credential-provider, network, stale-report, and response-validation
failures are normalized without exception text. The client never retries,
repairs state, publishes content, or logs credentials; those responsibilities
remain with the host.

## Durable polling scheduler

`integrations/website_localization_health_monitor.py` provides the optional
host-side retry and crash-resume layer. It wraps the same one-request client;
each durable lease therefore authorizes at most one authenticated health read.
The SQLite row records a poll attempt before network access, so a process crash
cannot lose or duplicate ownership. Another process waits for the live lease
and may recover it only after the configured expiry.

```python
import sqlite3
import time

from integrations.website_localization_health_monitor import (
    DurableWebsiteLocalizationHealthMonitor,
)

monitor = DurableWebsiteLocalizationHealthMonitor(
    sqlite3.connect("operator-health.sqlite3"),
    client,
    poll_interval_seconds=30,
    lease_seconds=30,
    max_consecutive_failures=5,
)
outcome = monitor.run_once("operator-worker-1", now=time.time())
```

Explicitly retryable client failures use capped exponential backoff. A
non-retryable failure, or exhaustion of the configured consecutive-failure
ceiling, moves the scheduler to `failed`; it makes no further request until an
operator calls `rearm()` after remediation. A valid `blocked` report is not a
transport failure: its status and reasons are recorded and the next ordinary
poll remains scheduled.

The durable database never stores the report payload. It retains only the
canonical SHA-256, aggregate report status and timestamp, sorted stable reason
codes, component/provider/website-version counts, scheduling state and the
last stable client error. In particular, it stores no site, event, version,
plan, locale or provider identifiers, no credential, and no source or target
content. Schema or row tampering blocks before a client call. `status()` is
read-only and marks an expired lease without implicitly claiming it.

`run_forever(worker_id, clock=..., stop_event=...)` supplies the corresponding
long-running synchronous loop. It performs at most one client read per claimed
lease, derives its next wait from the durable poll or lease deadline, clamps
that wait to the configured host wake interval, and uses the host event's
interruptible `wait()` rather than sleeping. A stop event that is already set
returns before schema or row access. Invalid clocks and stop-event contracts
fail closed. The method does not create a thread, daemonize, install a signal
handler, close SQLite, or choose process ownership; those remain explicit host
responsibilities.

`health(now=...)` returns
`blun.website-localization-health-poller.v1`. Initial operation without a
validated report, overdue scheduled poll, retry wait, and an expired lease are
`degraded`; a terminal scheduler state or last valid blocked service
assessment is `blocked`.
Otherwise a current scheduled or live-lease state backed by a healthy report
is `healthy`. The snapshot contains due/lease state, bounded attempts, next
action time, last stable client error and the same content-free report summary
stored durably. It never returns the complete remote report.

## Response and status semantics

A valid report is wrapped without changing the monitor's signed-state
decisions:

```json
{
  "schema": "blun.website-localization-health-http.v1",
  "report": {
    "schema": "blun.website-localization-health.v1",
    "checked_at": 1788962400.0,
    "status": "degraded",
    "components": [{
      "component": "supervisor",
      "status": "degraded",
      "reasons": ["supervisor.heartbeat_stale"],
      "counts": {"consecutive_blocked": 0}
    }],
    "providers": [],
    "website_versions": []
  }
}
```

The real monitor supplies non-empty component data. `healthy` and `degraded`
reports return HTTP `200`; a structurally valid `blocked` report returns HTTP
`503` while retaining the complete content-free report for diagnosis. Thus a
load balancer cannot mistake known corruption for health, while an operator
can distinguish a valid blocked assessment from an unavailable monitor.

Before delivery, the HTTP boundary independently validates the exact report
schema, finite check time, overall and component states, unique component and
event identities, non-negative counts, provider bindings, website lifecycle
states, locale failures, stable reason grammar, exact current check time, and a
four-megabyte response ceiling. Extra fields, free-form
reason prose, duplicate identities, impossible approval counts, invalid
provider output, malformed reports, or serialization failures produce only:

```json
{
  "schema": "blun.website-localization-health-http-error.v1",
  "error_code": "health.http.response_invalid",
  "retryable": true
}
```

All responses use `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`,
and `Referrer-Policy: no-referrer`. The host must additionally prevent access
logs from retaining operator credentials.

Premortem: an unauthenticated probe could enumerate sites, a malformed monitor
could leak customer prose, a blocked report could be returned as an ordinary
healthy response, authentication failure could fall through to state access,
or an external probe could be enabled without an operator boundary. Explicit
service-wide scope, authentication-before-monitor ordering, strict output
revalidation, HTTP `503` for blocked state, and pre-schema runtime validation
keep those paths fail-closed.
