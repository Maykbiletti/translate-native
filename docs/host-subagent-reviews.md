# Host-isolated website review delegation

`integrations/website_localization_subagents.py` supplies a provider-neutral
`HostSubagentProvider` for the existing localization worker and production
`provider_resolver`. It delegates **website translation reviews**, not ordinary
chat responses, to two separate host-managed executions. The provider-neutral
HTTPS host bridge described below is implemented; product-specific Claude,
Codex and other host launchers plus ordinary-response delegation remain
follow-up work.
Existing MCP, Skill, Stop and SubagentStop behavior is unchanged.

## Authenticated HTTPS host bridge

`integrations/website_localization_subagent_http.py` implements the `ReviewHost`
protocol for a remote trusted host. Configure one fixed HTTPS endpoint, an
authentication-header callback, a stable host ID and an attestation verifier
whose trust keys come from deployment configuration. The endpoint and trust
anchor never come from model output or from the response. Plain HTTP is
accepted only for explicitly enabled loopback tests.

```python
review_host = HTTPSReviewHost(
    "https://review-host.example/v1/subagent-reviews",
    authentication_headers,
    deployment_attestation_verifier,
    host_id="production-review-host-1",
)
```

Each `run_isolated` call performs exactly one request. It rejects redirects,
credentials in URLs, reserved authentication headers, duplicate JSON keys,
non-finite numbers, invalid UTF-8, oversized bodies and non-JSON responses.
Network errors, 408/425/429 and server errors are retryable by the durable
queue. Authentication rejection, redirects, idempotency conflicts, binding
errors and rejected attestations are terminal. Error bodies and authentication
values are never propagated into worker errors.

The request body is canonical UTF-8 JSON with this exact top-level shape:

```json
{
  "schema": "translate-native.host-subagent-http-request.v1",
  "host_id": "production-review-host-1",
  "execution_key": "<64 lowercase hex characters>",
  "task": "<the complete model-visible review task object>",
  "control": "<the complete host-only control object>",
  "request_sha256": "<SHA-256 of the other five fields>"
}
```

The strings describing objects above stand for the actual JSON objects. The
same execution key is sent as `Idempotency-Key`. The remote host must durably
reject reuse with a different body and return the same completed result for an
exact retry. `task` alone may enter the reviewer context. `control` is used by
the host to enforce identity, fresh context, empty tools, zero delegation,
deadline and token limits; it must never enter the model prompt or inherited
history. The client independently allowlists native-task fields before any
network access, so source fields and extra metadata fail closed.

The response contains exactly the response schema, host ID, execution key,
request digest, existing `{response, receipt}` result, and `attestation`.
Attestation has the exact fields `schema`, `algorithm`, `key_id`, and
`signature`. It signs canonical JSON containing:

```json
{
  "schema": "translate-native.host-subagent-http-attestation-payload.v1",
  "host_id": "production-review-host-1",
  "execution_key": "<execution key>",
  "request_sha256": "<request digest>",
  "result_sha256": "<digest of the complete response and receipt>",
  "completed": true
}
```

The verifier is deliberately provider-neutral. Production deployments should
use an asymmetric or managed trust verifier with pinned host identity and key
policy; the HMAC authority in tests is explicitly a fixture. After the
attestation passes, `verify_execution` accepts only the exact receipt and
control snapshot held by this client and verifies the attestation again. The
worker commits the complete host ID, request/result digests, receipt and
attestation into the existing review evidence hash, so a later approval is
bound to the authenticated remote execution. A
valid transport signature proves host origin and binding, not native-language
quality, reviewer independence or superiority over another system.

## Trust boundary and registration

The host, not the writing model, constructs the adapter with a creator provider,
a trusted `ReviewHost`, creator agent/session identities, model ID/version,
host-policy version, deadline and output-token ceiling. The host interface is a
Python protocol, not a tool exposed to models. Neither reviewer gets signing
keys, publication permissions, inherited messages, source lookup tools, or the
ability to spawn another reviewer.

```python
provider = HostSubagentProvider(
    creator, review_host,
    creator_id="writer", creator_session_id="host-assigned-session",
    model_id="configured-model", model_version="immutable-model-revision",
    host_policy_version="isolated-host-1",
    native_brief={"audience": "Business owners", "tone_profile": "Warm and concise",
                  "target_terms": []},
    timeout_seconds=60, max_output_tokens=4096,
)
# Put provider.provider_id and the exact model identity in the localization plan.
# The production provider_resolver must return a fresh equivalent adapter for
# each job attempt. Do not share the mutable adapter between jobs or threads.
```

The generated `host-subagents-v1-<policy digest>` provider identity binds the
delegation contract, originating identities, source-free brief, host policy and budgets. A changed
policy yields a different job/cache identity. Changing host implementation or
reviewer configuration requires a new immutable host-policy version. Never
reuse the creator's legacy provider ID for this mode. A delegated provider ID
without the worker evidence callback blocks before creation.

`native_brief` is mandatory and must be independently authored/approved by the
trusted host for target-only review. It contains audience, tone and target-only
terminology, never copied source passages, source glossary columns/notes,
creator reasoning or conversation history. Do not derive it from arbitrary
creator-supplied assets. Missing or malformed briefs block; content isolation
of this trusted configuration must be verified by the host before registration.

The host remains responsible for provider routing. If wrapping an existing HTTP
creator adapter, its endpoint must accept the registered composite provider
identity. This adapter does not silently rewrite provider identities.

## Execution contract

The creator receives only the creation call. Native review then runs in a fresh
context; fidelity review can start only after a valid native `PASS`. Reviewers
must have distinct host-assigned agent and session IDs, both different from the
creator. A `PASS` with low confidence does not remove escalation requirements.

`run_isolated(task, *, control)` returns exactly `response` and `receipt`:

- `task` is the **entire model-visible payload**. Native tasks contain the
  candidate, target locale/profile, content type, versioned locale-quality
  profile, optional commercial locale profile, and review response schema.
  The independently approved native brief supplies audience, tone and target
  terminology. Source glossary terms/notes, creator audience/tone free text, protected-term
  metadata, job IDs, creator IDs and previous review output are excluded.
  Source-aware fidelity tasks contain the complete source/candidate context.
- `control` is **host-only**. It carries the policy, exact worker-request digest,
  task digest, phase, stable execution key, and prior native receipt digest for
  fidelity. Do not append it to the model prompt or expose it through tools,
  shared memory, tracing, inherited sessions or attachments.
- `response` is the existing worker review schema, including confidence,
  blocking/major findings with excerpts/reasons and, where required, complete
  commercial evidence. Extra or malformed review fields are rejected by the
  worker. An unsupported locale or uncertain review must report low confidence,
  never invent a successful assessment.
- `receipt` has exactly the fields enforced by `_validate_receipt`: schema,
  execution key, request/task/response digests, phase, previous receipt digest,
  actual agent/session/model identities, and isolation settings. The host
  constructs it from execution facts, never by copying model claims.

`verify_execution(receipt, *, control)` must return the boolean `True` only when
the receipt exactly matches the host's authoritative completed-execution ledger.
Merely recomputing hashes, trusting a model-supplied identity, or checking the
receipt against itself is **not** verification. A remote implementation needs
authenticated transport and authenticated host evidence. Failed verification,
missing host support or malformed evidence blocks; there is no local model or
self-review fallback.

The host must enforce the deadline, output-token ceiling, fresh-context
isolation, empty tool list and zero delegation depth. It must cancel/terminate
timed-out executions and must not return success while they still run. This
synchronous Python adapter cannot terminate a misbehaving host implementation.
Reviewer machine output must not enter a user-delivery hook that recursively
starts another reviewer. This does not exempt ordinary user output from the
existing Guard.

## Retry, evidence and approval

One adapter instance handles one job attempt, with one creation and at most two
review calls, no internal correction loop and no automatic nested delegation.
Exceptions become content-free errors. The existing durable queue owns leases,
bounded retries, backoff and crash recovery. The host must durably deduplicate
`control.execution_key`, including across process restarts, and return the same
verified completed execution for an exact retry. Changed candidates, jobs,
locales, policies or prior native receipts produce different execution keys.

The worker's optional `verified_call_evidence` callback commits each validated
host receipt into its existing review-phase `response_sha256`:

```json
{
  "schema": "translate-native.host-review-commitment.v1",
  "response": "the complete structured review object",
  "host_evidence": "the exact host-verified receipt object"
}
```

The two strings above describe objects, not literal wire values. The digest uses
canonical UTF-8 JSON with sorted keys, compact separators and finite numbers.
The queue hashes the complete worker result; evidence requests and signed
approvals bind that result hash. Existing strict result schemas are unchanged.
The trusted host must retain receipts and review records under appropriate
access/retention controls so these commitments remain auditable. Operational
results contain only digests, not reviewer prose or raw execution identities.
A digest proves binding, not that a human/native-quality review occurred.

Deterministic structure checks, independent evidence verification and final
signing remain in the existing release pipeline. Two isolated executions of
the same model do **not** count as a second independent adapter, and this
adapter creates no independent-model or qualified-human receipt. The host's
independence registry must not treat aliases or composite provider IDs as proof
of independent models. Low confidence still requires independent evidence;
legal content still requires qualified-human evidence. No averaging overrides
a major or blocking defect. Text changes invalidate the bound result.

## Verification and limits

`tests/test_website_localization_subagents.py` exercises the registered adapter
through worker, durable queue restart, evidence coordinator and signed release.
It uses synthetic Finnish and Maltese text and an in-memory host ledger. These
fixtures test protocol behavior, **not** linguistic acceptance, concrete host
sandboxing or superiority over DeepL. A deployment must implement and verify
the host contract above before enabling this provider identity. No live host
configuration is installed by this change.
