# Localization receipt-verifier HTTPS contract

`integrations/website_localization_receipt_verifier_http.py` is a
provider-neutral network implementation of the release store's receipt
verifier boundary. It does not issue evidence, choose a model, hold a signing
key, approve a locale, or publish content. It asks one host-configured service
whether one opaque receipt authenticates one exact canonical release binding.

## Host configuration

Construct `HTTPReceiptVerifierAdapter` with:

- one fixed absolute HTTPS endpoint;
- a callback returning authentication headers at call time;
- an optional transport implementation; and
- a timeout from 0 to 300 seconds.

Plain HTTP is rejected except for an explicitly enabled loopback endpoint used
for local development. User information, fragments, queries, control
characters, ambiguous paths, unsafe authentication headers, redirects, and
authentication attempts to replace protocol headers are rejected. The adapter
makes exactly one transport attempt. Durable release coordination owns every
retry and its attempt limit.

## Request

The adapter canonicalizes the complete
`blun.localization-quality-receipt-binding.v2` object and validates its native
Unicode text, hashes, locales, content type, glossary and policy versions,
provider/model identities, software version, two-pass confidence, locale
quality profile, optional commercial profile and exact targeted-review summary,
escalation requirements, and review purpose. It then sends HTTP POST with JSON content type and these
protected headers:

- `Idempotency-Key`;
- `X-Localization-Receipt-Request-Id`; and
- `X-Localization-Receipt-Request-Sha256`.

The body is exactly:

```json
{
  "schema": "blun.localization-receipt-verification-http-request.v1",
  "request_id": "blun-l10n-receipt-<sha256>",
  "binding_sha256": "<canonical binding hash>",
  "receipt_sha256": "<opaque receipt hash>",
  "binding": {"schema": "blun.localization-quality-receipt-binding.v2"},
  "receipt": "<opaque receipt>"
}
```

The deterministic request ID is derived from both hashes. Identical retries
therefore address the same verification operation, while any changed receipt
or binding dimension receives a different identity. The remote service must
treat an idempotency-key collision with different bytes as a terminal error.

## Response

Success uses HTTP 200 and JSON content type with exactly:

```json
{
  "schema": "blun.localization-receipt-verification-http-response.v1",
  "request_id": "<same request ID>",
  "request_sha256": "<exact request-body hash>",
  "binding_sha256": "<same binding hash>",
  "receipt_sha256": "<same receipt hash>",
  "verified": true
}
```

`verified: false` is a valid negative decision and permanently blocks release.
Wrong bindings, duplicate keys, non-finite values, a UTF-8 byte-order mark,
incorrect content lengths, oversized bodies, extra fields, redirects, and
terminal HTTP statuses fail closed. Responses never carry reviewer prose.

Network failures, HTTP 408/425/429 and HTTP 5xx, malformed successful response
transport, and temporary parser failures are classified as retryable but are
not retried inside the adapter. The release coordinator records only a stable,
content-free failure code and applies its existing durable backoff and maximum
attempt policy. Authentication, invalid requests, redirects, other HTTP
statuses, binding mismatches, and explicit negative verdicts are terminal.

The same adapter can be configured separately for quality, qualified-human,
and independent-model receipts. Purpose is part of the signed binding, so a
receipt accepted for one route cannot satisfy another route.
