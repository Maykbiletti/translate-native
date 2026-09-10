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
