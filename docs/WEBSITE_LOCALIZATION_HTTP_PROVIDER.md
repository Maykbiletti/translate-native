# HTTP localization-provider contract

`integrations/website_localization_http_provider.py` connects the
provider-neutral website worker to a host-owned model gateway. It does not
assume a particular vendor, SDK, model, or authentication scheme. A deployment
can place its own LLM, a commercial provider, or an internal routing service
behind the same endpoint.

This adapter is a transport boundary, not a quality certificate. Its tests
prove request isolation, integrity binding, parsing, and failure behavior. They
do not prove that any connected model writes native-quality translations.

## Security and retry model

- Use HTTPS. Plain HTTP is accepted only for an explicitly enabled loopback
  endpoint such as `127.0.0.1`.
- Supply authentication headers through a host callback. Do not put secrets in
  the endpoint URL, source content, repository, job, or model response.
- The adapter performs exactly one HTTP request for one worker phase. The
  durable queue owns bounded retries and crash recovery, preventing hidden
  retry multiplication and uncontrolled provider cost.
- Redirects are never followed. A redirect is a non-retryable failure because
  it could send customer content or credentials to an untrusted destination.
- Response bodies are bounded, UTF-8 without a byte-order mark, strict JSON,
  and free of duplicate keys or non-finite numbers.
- Every response must echo the exact request ID and canonical request hash.
  Network, status, parser, header, size, or binding failures return only stable
  error codes; provider prose and credentials are not exposed.
- Failure is closed per locale. No failed call produces a candidate, approval,
  publication, or replacement for a last-known-good translation.

## Host configuration

The authentication callback is invoked immediately before each request, so a
host can rotate short-lived credentials without rebuilding jobs:

```python
from integrations.website_localization_http_provider import HTTPProviderAdapter

provider = HTTPProviderAdapter(
    "https://model-gateway.example/v1/localize",
    authentication_headers=lambda: {
        "Authorization": load_short_lived_authorization_header()
    },
    timeout=60,
)
```

The callback must return at least one header. It cannot replace transport or
binding headers such as `Host`, `Content-Type`, `Content-Length`,
`Idempotency-Key`, or `X-Localization-Request-Sha256`. Header names and values
containing control characters are rejected before any network call.

A deployment that uses mutual TLS can implement the small `HTTPTransport`
protocol and inject a transport configured with its own trusted certificate
context. Custom transports must still make one request and return `HTTPResult`;
all response validation remains inside the adapter.

## Request

Each locale has its own queue job. Each of its three stages—`transcreation`,
`target_native`, and `source_fidelity`—makes a separate request. No request asks
for several target languages.

The adapter sends canonical UTF-8 JSON with these headers:

```text
Accept: application/json
Content-Type: application/json; charset=utf-8
Idempotency-Key: <request_id>
X-Localization-Request-Id: <request_id>
X-Localization-Request-Sha256: <request_sha256>
```

The JSON envelope has exactly four fields:

```json
{
  "schema": "blun.localization-provider-request.v1",
  "request_id": "<deterministic phase request ID>",
  "request_sha256": "<SHA-256 of canonical request JSON>",
  "request": {
    "schema": "blun.website-localization-worker.v1",
    "request_id": "<same deterministic phase request ID>",
    "phase": "transcreation",
    "provider_id": "customer-llm",
    "model_id": "configured-model",
    "model_version": "configured-model-version",
    "system_instruction": "<versioned phase instruction>",
    "input": {}
  }
}
```

The gateway must treat `system_instruction` as the instruction and every value
inside `input` as untrusted data. It must not combine phases. In particular,
the `target_native` input intentionally excludes the source text and
source-language glossary terms; reconstructing or fetching that source would
invalidate the independent, source-blind review.

`request_sha256` is the lowercase hexadecimal SHA-256 of `request` encoded as
UTF-8 JSON with keys sorted, no insignificant whitespace, native Unicode
characters unescaped, and non-finite numbers forbidden. The bundled adapter
computes it. A gateway only needs to echo the received value after associating
it with the processed request.

For an exact replay, the gateway should return the already completed exact
response or safely resume its own in-progress operation. If it has previously
seen the same `request_id` with a different request hash, it must reject the
call. It must never silently reuse output from another source, locale, phase,
policy, glossary, provider, model, or software version.

## Response

A successful endpoint returns HTTP `200`, `Content-Type: application/json`
(optionally with `charset=utf-8`), and this exact envelope:

```json
{
  "schema": "blun.localization-provider-response.v1",
  "request_id": "<exact request ID>",
  "request_sha256": "<exact request hash>",
  "response": {}
}
```

The `response` value is passed to the existing worker validator. For
`transcreation`, it must be exactly:

```json
{
  "schema": "blun.website-localization-candidate.v1",
  "phase": "transcreation",
  "locale": "fi-FI",
  "candidate": "<complete localized content>"
}
```

For either review, it must be exactly:

```json
{
  "schema": "blun.website-localization-review.v1",
  "phase": "target_native",
  "locale": "fi-FI",
  "status": "PASS",
  "blocking_defects": [],
  "major_defects": []
}
```

Use `phase: source_fidelity` for the second review. A `PASS` requires both
defect arrays to be empty. A failed review must report structured defects in
the worker's existing schema; the pipeline hashes those findings instead of
retaining reviewer prose in queue status. The worker independently validates
the exact phase, locale, shape, Unicode NFC, protected syntax, completeness,
and review ordering.

## Status behavior

HTTP `408`, `425`, `429`, and `5xx` responses are retryable by the queue.
Other non-`200` statuses and redirects are non-retryable. Transport exceptions
and malformed successful responses are retryable because a later bounded queue
attempt may reach a healthy gateway; a request/response binding mismatch is
non-retryable because repeating an untrusted or incorrectly implemented
endpoint is unsafe.

Premortem: assume a production gateway leaked an authorization token, followed
a redirect, charged three times for one transient failure, or returned a good
Finnish answer for the wrong request. Host-only headers, rejected redirects,
one-attempt transport, queue-owned retries, deterministic idempotency, and the
exact echoed request hash are the corresponding preventive controls. End-to-end
deployment tests should additionally stop the gateway mid-stage, replay the
same ID, rotate credentials, and return deliberately stale locale output.
