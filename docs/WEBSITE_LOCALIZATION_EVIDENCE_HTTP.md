# Website localization quality-evidence HTTP contract

`integrations/website_localization_evidence_http.py` connects the production
release coordinator to a host-owned quality service without choosing a model,
review vendor, or credential format. It performs exactly one HTTP attempt for
one already leased locale-evidence job. The durable
`QualityEvidenceStateStore` remains the only retry and crash-recovery owner.

Transport conformance proves request integrity and fail-closed behavior. It
does not prove that a translation is native, publishable, or better than an
external baseline.

## Host configuration

Construct `HTTPEvidenceProviderAdapter` with:

- one fixed HTTPS endpoint;
- a callback returning authentication headers at call time;
- an optional host transport and timeout;
- `allow_loopback_http=True` only for an explicit `localhost`, `127.0.0.1`, or
  `::1` development endpoint.

The endpoint cannot contain user information, a query, or a fragment.
Redirects are never followed, so credentials cannot be forwarded to another
origin. Authentication values must be ASCII control-free strings. The host
cannot override `Host`, framing, content, idempotency, or binding headers.
Neither credentials nor remote response prose appears in adapter errors.

## Request

The adapter accepts only the coordinator's exact immutable
`blun.localization-quality-evidence-request.v13` object. It validates the
complete field set, derives the request ID, and checks SHA-256 values, locale
profile, model identity,
confidence decisions, and, for commercial content, the exact locale-specific
commercial quality-profile binding plus its content-free targeted-review
summary, UTF-8 size, Unicode NFC, and the source and target text hashes before
authentication or transport.

It sends one canonical UTF-8 JSON document:

```json
{
  "schema": "blun.localization-quality-evidence-http-request.v1",
  "request_id": "blun-l10n-evidence-<64 lowercase hexadecimal characters>",
  "request_sha256": "<SHA-256 of the canonical inner request>",
  "request": {
    "schema": "blun.localization-quality-evidence-request.v13",
    "request_id": "<same request ID>",
    "source_locale": "en-IE",
    "target_locale": "fi-FI",
    "source_text": "<complete source>",
    "target_text": "<complete candidate>",
    "result_sha256": "<validated durable worker-result hash>",
    "source_sha256": "<source-text hash>",
    "target_sha256": "<target-text hash>",
    "content_type": "headline",
    "commercial_review_routing": null,
    "commercial_review_routing_contract_sha256": null,
    "commercial_review_routing_contract": null,
    "commercial_review_resolution_contract_sha256": null,
    "quality_profile": {
      "locale": "fi-FI",
      "version": "<locale profile version>",
      "sha256": "<locale profile hash>"
    }
  }
}
```

The abbreviated example omits other required inner fields for readability.
Production requests always contain exactly the full v13 field set. Commercial
requests include `commercial_profile`, the matching `commercial_review`
summary, and `quality_profile.commercial` with the same profile identifier plus
its exact locale-specific version and SHA-256 digest. The summary binds the
exact advertised commercial review-evidence-contract SHA-256, so a prior
structurally valid report cannot be reinterpreted under a changed contract. An unresolved commercial
summary additionally carries the canonical advertised resolution-contract
SHA-256; this field is part of the deterministic request ID. Verified
commercial results and non-commercial requests require it to be `null`.
Every commercial request also carries
`commercial_review_routing_contract_sha256`, even when the commercial review
is already verified and no private offer route is needed. This lets the
evidence provider issue the receipt against the same exact public contract
digest that the receipt verifier, signed approval, and CMS evidence later
require. Non-commercial requests require this field to be `null`.
An unresolved commercial request also carries a private
`commercial_review_routing` object. It maps every zero-based opaque offer index
to its ordered, non-overlapping source and target Unicode code-point spans and
binds both complete text lengths. The object contains no configured offer ID,
text, price, brand, or reviewer prose. It also carries the exact SHA-256 of the
separately advertised machine-readable routing contract. The sibling
`commercial_review_routing_contract` field contains that complete content-free
contract, so a remote reviewer does not depend on separate capability
discovery. The adapter reconstructs the canonical object and validates the
route against the exact source, target, summary, and profile before
authentication or transport. Route, digest, and full contract are part of the
deterministic request ID. Verified commercial results and non-commercial
requests require both route and routing contract to be `null`.
Non-commercial requests require every commercial field to be `null` and forbid
the nested commercial quality profile.
Source and
target text are intentionally present because this external step verifies the
existing source-blind native review and the separate source-aware fidelity
review. The service must preserve their confidentiality.

The suffix of `request_id` is the SHA-256 of canonical UTF-8 JSON containing
exactly `schema`, `evidence_revision`, `event_id`, `plan_id`, `job_id`,
`result_sha256`, both text hashes, both locales, `content_type`, glossary and
policy versions, provider/model identity, software version, both confidence
decisions, the complete locale quality profile, commercial profile and review,
the routing-contract digest, the conditional commercial review-routing object,
its complete routing contract, the resolution-contract hash, and both escalation
flags. The full texts and `request_id` itself are excluded from that identity;
their exact bytes are already bound by `source_sha256` and `target_sha256`.
The durable store and HTTP adapter independently recompute this same identity.

The adapter adds:

```text
Content-Type: application/json; charset=utf-8
Accept: application/json
Idempotency-Key: <request_id>
X-Localization-Evidence-Request-Id: <request_id>
X-Localization-Evidence-Request-Sha256: <request_sha256>
```

The receiver must treat the idempotency key and canonical request digest as one
immutable operation. Reusing the ID for different bytes must fail closed.

## Response

A successful service returns HTTP 200, JSON content type, and exactly:

```json
{
  "schema": "blun.localization-quality-evidence-http-response.v1",
  "request_id": "<same request ID>",
  "request_sha256": "<same canonical request hash>",
  "result_sha256": "<same validated worker-result hash>",
  "evidence": {
    "schema": "blun.localization-quality-evidence-response.v2",
    "request_id": "<same request ID>",
    "result_sha256": "<same validated worker-result hash>",
    "quality_receipt": "<host-verifiable purpose-bound receipt>",
    "human_review_receipt": null,
    "independent_model_review": null
  }
}
```

The adapter rejects extra or missing fields, duplicate JSON keys, non-finite
numbers, a UTF-8 byte-order mark, wrong bindings, ambiguous content types,
incorrect lengths, and oversized bodies. The release coordinator then applies
its existing independent receipt checks. Each opaque receipt must verify
against the complete canonical
`blun.localization-quality-receipt-binding.v9` object supplied by the release
coordinator, including the review purpose, job and result hashes, both texts
and locales, content type, glossary and policy versions, provider/model and
software identities, locale quality and commercial profiles, the exact
commercial review scope, canonical resolution-contract SHA-256, confidence,
the exact public offer-routing-contract SHA-256, the private offer-routing
context, escalation requirements, and the exact
evidence request ID and revision from
this response. Reuse across
a changed field or between quality,
qualified-human, and independent-model review purposes must fail closed.
Legal content still requires a
separately verified qualified-human receipt. Low-confidence non-legal content
still requires exactly one qualified-human receipt or a differently identified
and independently verified second model review.

## Failure and retry contract

The adapter raises only stable, content-free `HTTPEvidenceProviderFailed`
codes. Network errors, HTTP 408/425/429, HTTP 5xx, malformed transport data,
and response parsing failures are retryable. Redirects, other HTTP statuses,
unsafe requests, and binding mismatches are permanent. Every invocation makes
at most one transport attempt.

The coordinator records the stable code, releases or terminates the exact
token-bound evidence lease, and applies its configured bounded exponential
backoff. A retry uses the same deterministic request ID. A last known good
translation is never replaced, and no publication is prepared until all
policy-required locales hold valid signed approvals.
