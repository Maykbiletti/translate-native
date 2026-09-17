# Host-isolated review delegation

`integrations/website_localization_subagents.py` supplies a provider-neutral
`HostSubagentProvider` for the existing localization worker and production
`provider_resolver`. It delegates website translations to two separate
host-managed reviewers. `integrations/response_subagent_review.py` uses the
same provider-neutral host boundary for exactly one source-blind review of an
ordinary response. The authenticated HTTPS bridge described below supports
both contracts. The Guard service exposes a provider-neutral factory slot and
a bundled protected HTTPS runtime for deployment-owned Claude, Codex or other
host implementations. Neither path activates a provider or creates secrets.
Existing translation, Stop and SubagentStop enforcement stays authoritative.

## Ordinary-response review

For `release_response`, the trusted host-side `PreToolUse` boundary asks the
isolated Guard for an opaque one-time review context. The ticket is HMAC-bound
to the current Guard boot and protocol version, exact target hash, locale,
content type, current session epoch and creator agent. The Guard reserves its
short-lived nonce before external work, so a timeout or lost reply cannot cause
ambiguous reuse. Text, locale, content-type, identity, epoch, version, boot,
signature or replay mismatches block before the reviewer runs.

`ResponseSubagentReviewer` submits a `target_native` task with schema
`translate-native.response-subagent-review.v1`. Its model-visible input is
limited to the candidate, target locale, content type, versioned quality
profile, response schema and independently approved target-only native brief.
There is no source field, prior conversation, creator identity, inherited
context, tool access or delegation depth. The host-only control fixes creator
identity, model/version, policy, budgets and exact request/task hashes.

The reviewer returns `translate-native.response-native-review.v1` with exact
phase and locale, `PASS` or `FAIL`, high or low confidence, structured findings
(`code`, `severity`, `reason`, `uncertainty`) and unresolved uncertainties.
Only a host-verified high-confidence `PASS` without major/blocking defects or
uncertainty can continue. Reviewer agent and session must both differ from the
creator. The Guard—not the reviewer—runs deterministic Unicode/script checks
and signs the final response receipt with the complete review-evidence digest.
That receipt also binds the creator session, session epoch, creator agent and
Guard boot. The Guard rechecks the epoch after the external review while
holding the signing lock. Local signing without a configured host is disabled;
readiness reports the missing reviewer and release remains fail-closed.

The MCP schema intentionally does not require `review_context_token` from the
model. Claude's trusted `PreToolUse` hook obtains and injects it after the tool
call is formed. The isolated service still requires an authentic, unused token
for the exact request, so omission or direct calls without hook injection block.

The normal Guard process accepts a deployment factory without learning any
specific provider:

```console
python integrations/guard_service.py \
  --response-review-factory my_product.review_host:build_response_reviewer
```

The trusted zero-argument callable must return a configured object implementing
`review(...)`. Invalid references or objects prevent service startup; omitting
the option starts in the explicit blocked state. The factory owns endpoint,
credentials, pinned attestation verifier, model/profile versions and target-only
brief configuration. It receives no Guard signing key and must not expose those
values to the reviewer task.

### Bundled protected HTTPS factory

Deployments that implement the authenticated HTTPS host contract do not need
custom Python glue. Copy `integrations/response-review.example.json` to an
operator-owned absolute path, replace every placeholder, keep the JSON, bearer
token and HMAC attestation secret in separate owner-only regular files, and
start the Guard with:

```console
python integrations/guard_service.py \
  --response-review-config /absolute/protected/path/response-review.json
```

The schema `translate-native.response-review-https-runtime.v1` is closed. It
pins the endpoint, host ID, bearer-token file, `hmac-sha256` attestation key ID
and secret file, immutable model/policy/profile/prompt/software versions,
target-only native brief, deadline, output-token ceiling and concurrency cap.
All paths must be absolute. On POSIX systems each file must be owned by the
service user, have one link and mode `0600`; its directory is walked without
following links and its identity is rechecked after each read. A linked,
hard-linked, oversized, broadly readable, replaced, malformed or duplicate-key
configuration blocks service startup. Credentials are loaded only by the host
adapter and never enter task JSON, evidence or errors.

The maximum review deadline is 60 seconds, below the Guard client's 75-second
ordinary-response deadline. The bundled runtime performs the single HTTPS
exchange in a dedicated credential-minimal process and kills and reaps it at
that wall deadline; a slowly streaming socket cannot extend the review
indefinitely. `max_concurrent_reviews` accepts 1–32 and rejects excess work
immediately as retryable host capacity; it does not create an unbounded local
queue or automatic retry loop. The stable execution key and remote host ledger
remain responsible for exact lost-response deduplication. Concurrent local
replies for the same key are committed under one atomic lock; a different
second attested result is an idempotency conflict, never alternate evidence.

The installer never creates this deployment state. When the standard path
`~/.config/blun-language-guard/response-review.json` exists—including as an
unsafe or dangling link—the generated persistent Guard command passes it
explicitly. The Guard then validates it and fails closed instead of silently
starting an unreviewed response path. Re-run installation or runtime refresh
after intentionally adding or removing the file so the service definition
reflects that operator action.

HMAC authenticates a trusted host that shares the pinned secret; it does not
prove linguistic quality or model independence. Protect and rotate that secret
as production trust material. A deployment requiring an asymmetric or managed
attestation authority can continue to use
`--response-review-factory package.module:callable` with the same adapter
contract. The bundled runtime connects to an existing review host; it does not
implement that host's model-specific subagent launcher.

Stop and SubagentStop consume already signed one-time delivery grants. They do
not invoke review agents, and review tasks have empty tools and zero delegation
depth, so internal reports cannot recurse through user-output hooks or become
publishable user output.

A response receipt is evidence for the Guard, not a bearer publication token.
The generic `verify` operation rejects response delivery even when the receipt
is otherwise authentic. A trusted host must use `authorize_delivery` and then
`consume_delivery` with the exact session ID, current session epoch, creator
agent and channel. Both steps are checked against the receipt and the live
Guard state; the grant is one-time and Guard-boot-bound. Translation receipts
remain available to the existing portable verification path.

### Durable reference host endpoint

`integrations/website_localization_subagent_host.py` implements the server half
of the authenticated HTTPS bridge. Its WSGI boundary authenticates the fixed
bearer before reading or parsing a request body. A closed `PinnedReviewPolicy`
then resolves the exact schema, phase, locale, content type and complete
target-language task-policy digest. The target-native digest covers every task
field except the candidate itself, so source text hidden in a brief, profile or
other nested metadata blocks before launcher access.

The trusted route—not the model or caller—assigns agent identity, session,
reviewer role, model version and budgets. The launcher receives only that
assignment and a reduced model task. Target-native input contains no source,
creator context, inherited messages, tools, credentials, journal or signing
authority. Fidelity input contains the source and candidate needed for its
separate comparison, but excludes internal job and policy identifiers.

A launcher implements both methods:

```python
execute_idempotent(
    assignment, model_input,
    deadline_seconds=deadline_seconds,
    max_output_tokens=max_output_tokens,
)
reconcile(
    assignment, model_input,
    deadline_seconds=deadline_seconds,
    max_output_tokens=max_output_tokens,
)
```

The current contract passes the same reduced `model_input`, deadline and output
ceiling to `reconcile(...)` locally so the launcher can reconstruct the original
execute digest and exact usage limits. The reconcile HTTP request itself still
contains no model content.

`execute_idempotent` must atomically deduplicate the physical model start by
`assignment.execution_key` across processes. SQLite lease generations fence
stale commits, but cannot stop an old paused process from crossing an external
start boundary after its lease expires. `reconcile` returns exactly one of
`completed`, `not_started`, `running`, `unknown` or `cancel_pending`; only a
trustworthy `not_started` result may start work after recovery.

The owner-controlled SQLite journal binds the authenticated principal, host,
request, candidate, locale, phase, provider, host policy and complete review
sequence. It stores the complete validated HMAC-attested reply before HTTP 200,
replays exact completed requests byte-for-byte, and rejects a changed request
under the same execution key. Permanent identity, response-contract and
reconciliation errors use non-retryable HTTP status. Ambiguous launcher loss
remains retryable and must be reconciled without inventing a review.

Source-fidelity work requires the exact completed native receipt for the same
sequence, principal and host, with distinct reviewer agent and session
identities. The host validates the complete phase-specific structured response
before signing; it does not treat an eventual downstream rejection as license
to attest malformed reviewer output. Commercial fidelity retains its pinned
public evidence contract and is validated with the same portable commercial
validator used by the worker before the host attests it.

The endpoint is provider-neutral and deliberately does not contain a real
model-specific launcher. Finnish and Maltese fixtures exercise the protocol,
not native-language quality, model independence or superiority over DeepL.

### Protected host runtime

`integrations/website_localization_subagent_host_runtime.py` composes that
endpoint into a deployable, process-bound WSGI runtime without selecting a
model provider. Copy `integrations/subagent-review-host.example.json` and
`integrations/subagent-review-launcher.example.json` to an operator-owned
absolute directory. The directory must not be writable outside its owner; the
configuration, bearer token, HMAC secret, launcher configuration and launcher
factory source must be owner-only regular files with one hard link. Replace
every example digest and identifier with the exact deployment values.

The closed host configuration pins the host and attestation identities,
SQLite journal, lease, complete review routes, reviewer identities, model and
policy versions, launcher factory, source-file digest, launcher identity and
launcher configuration. The factory is a trusted deployment source file plus
an exact callable name. The runtime hashes its exact bytes before compiling
those same bytes directly, so package initializers and stale Python bytecode
cannot run in place of the reviewed source. The callable receives only a copy
of the launcher's `settings` object and must return an object exposing the configured
`launcher_id`, `launcher_version`, atomic `execute_idempotent(...)` and
`reconcile(...)`. It receives no HTTP credential, HMAC signer, host journal,
route table or publication capability. Absolute imports made by that source
remain trusted deployment dependencies and must be protected and version-pinned
separately; one source digest cannot prove an entire dependency graph.

Ledger creation is explicit and happens only after the protected configuration,
launcher code, launcher result and secrets have passed preflight:

```console
python integrations/website_localization_subagent_host_runtime.py \
  --config /absolute/protected/path/review-host.json \
  --initialize-ledger --check
```

Normal starts omit `--initialize-ledger`; a missing ledger then blocks instead
of silently erasing recovery history. The journal stores one durable digest of
the exact host, authentication and attestation identities, routes, reviewer
assignments, launcher code/configuration and lease. A later configuration or
key change against that journal blocks startup rather than replaying historical
evidence under a new deployment identity. Deliberate rotation therefore needs
a separately reviewed journal migration or a new explicitly initialized
journal; this runtime does not rewrite the binding.

The bundled server listens only on loopback and is intended to sit behind a
trusted HTTPS terminator:

```console
python integrations/website_localization_subagent_host_runtime.py \
  --config /absolute/protected/path/review-host.json \
  --listen-host 127.0.0.1 --listen-port 47641
```

The host configuration must explicitly permit loopback HTTP for that internal
hop. Do not expose this listener directly. A forked or closed runtime rejects
requests before journal replay or launcher access. Startup never invents a
launcher, reviewer result, credential or signing key.

The bundled
`integrations/website_localization_subagent_launcher_http.py` is the standard
provider-neutral launcher factory for this runtime. Its protected settings pin
one HTTPS executor endpoint, launcher and executor identities, one owner-only
digest-bound bearer token, request timeout, input-byte ceiling, named cost unit,
cost ceiling, non-queuing concurrency cap and finite polling budget. Plain HTTP
is allowed only for an explicitly enabled loopback test. Authentication appears
only in the transport header and never in the JSON body or model-visible input.
The launcher is self-contained: its protected factory digest covers the
embedded transport worker, and each request starts that immutable worker with
Python isolation instead of rereading mutable factory bytes or inherited Python
startup paths.

For `execute`, the launcher sends the exact host assignment, the already reduced
`model_input`, hard deadline, input-byte, output-token and cost ceilings. For `reconcile`, it sends
the same assignment without model content. Both operations bind canonical
request bytes to `X-Subagent-Request-Sha256`, and send the exact execution key as
both `Idempotency-Key` and `X-Subagent-Execution-Key`. The executor response must
echo the operation, request digest, execution key and all pinned service
identities. A completed result must contain the exact execution object expected
by the host and an exact usage object with the original execute-request digest,
pinned cost unit, bounded integer cost, exact input-byte count and bounded
output-token count. Recovery reconstructs and checks those same exact values.
The host stores the usage object in its signed receipt, so the worker and Guard
evidence bind it as well. Missing, boolean, negative, excessive, foreign or
rebound usage values block. `running`, `unknown`, `cancel_pending` and
`not_started` never become review evidence.

The executor, not this transport adapter, owns the physical host-subagent
facility. It must atomically deduplicate starts by the execution key, enforce the
deadline, byte, token and cost ceilings, retain completed results and usage for reconciliation and run
each reviewer with empty inherited history, no tools and zero delegation depth.
If that host facility is unavailable, the launcher returns a content-free
blocked state rather than inventing a review. The launcher never receives the
Guard signer, host attestation key, publication rights, route table or SQLite
journal. `max_concurrent_executions` rejects a new physical start immediately as
retryable when all slots are occupied; it does not create an unbounded waiting
queue. A running or ambiguous key retains its local slot until reconciliation
proves `completed` or `not_started`. The exact cap is also in the signed execute
request and the trusted executor must enforce it atomically across launcher and
host-process restarts; the local bound is not a substitute for that durable
executor boundary.

Operators still must independently test their real executor isolation,
provider credentials and the mapping from provider billing into the pinned
deployment cost unit. The launcher's production request runs in a killable
one-shot process; the parent terminates it at the remaining wall deadline,
including slow response streams. Synthetic Finnish and
Maltese runtime tests prove the configuration, source isolation, restart replay
and fail-closed bindings only; they are not native-language evidence and make no
DeepL superiority claim.

## Durable executor and backend contract

`integrations/website_localization_subagent_executor.py` terminates the
launcher's `/v1/subagent-executions` contract. It authenticates before reading
the body, validates the canonical request and all duplicate header bindings,
accepts only one pinned launcher/executor identity, and resolves the host's
assignment through a locally pinned route. Each route repeats the exact
reviewer, model, locale, content-type and host-policy assignment and the
candidate-neutral task-policy SHA-256 used by the review host. Changing the
system instruction, target profile, audience, tone or other permitted native
brief data therefore blocks even when the attacker keeps every field name
valid. The candidate itself remains dynamic and is bound by the execution key.

The executor maintains its own owner-controlled SQLite journal. Under one
`BEGIN IMMEDIATE` transaction it checks the authenticated principal, full
execute request, assignment and global capacity before committing `dispatching`.
That state has no timeout-based takeover: while its owner could still cross the
physical-start boundary, read-only reconciliation may recover a validated
backend completion but preserves the dispatch barrier through errors and every
non-completed observation, including `not_started`. `running`, `unknown` and
`cancel_pending` also keep their reservation. Outside that unresolved dispatch
window, only an exact completed result or a backend-confirmed `not_started`
state releases it. This deliberately favors fail-closed capacity loss over
duplicate paid review work.

The deployment backend implements:

```python
execute_idempotent(
    assignment, model_input,
    execute_request_sha256=execute_request_sha256,
    budgets=budgets,
)
reconcile(
    assignment,
    execute_request_sha256=execute_request_sha256,
)
```

It must atomically deduplicate physical work by the host-owned execution key,
start each reviewer with empty inherited history, no tools and zero delegation
depth, and return one of `completed`, `not_started`, `running`, `unknown` or
`cancel_pending`. Only `completed` carries execution and usage. The executor,
not model output, verifies reviewer identity, session, phase, role, model,
isolation flags, original execute digest, exact input bytes, cost unit, bounded
cost and bounded output tokens before committing evidence. The current closed
status set has no terminal failed/aborted state; such outcomes must remain
`unknown` and quarantined rather than being relabeled `not_started`. Adding
automatic terminal-failure capacity release requires a versioned change across
executor, launcher and review host.

`integrations/website_localization_subagent_executor_runtime.py` composes this
endpoint from protected configuration. Copy
`integrations/subagent-review-executor.example.json` and
`integrations/subagent-review-backend.example.json` into an owner-only absolute
directory, supply an owner-only bearer token and a digest-pinned backend factory,
then initialize the journal explicitly:

```console
python integrations/website_localization_subagent_executor_runtime.py \
  --config /absolute/protected/path/subagent-executor.json \
  --initialize-ledger --check
```

Normal starts omit `--initialize-ledger`; missing, foreign, permission-weakened,
replaced or deployment-drifted state blocks. The backend factory receives only
its copied settings. It never receives the executor credential, route table,
journal, review-host attestation key, Guard signer or publication capability.
The bundled server listens only on loopback behind a trusted HTTPS terminator.
A concrete backend may connect to a host's supported subagent facility, but it
must independently prove isolation, billing mapping, cancellation semantics and
provider credentials. Synthetic Finnish and Maltese fixtures are explicitly
protocol tests, not native-language or DeepL-quality evidence.

### Standard HTTPS facility backend

`integrations/website_localization_subagent_backend_http.py` is the bundled
`ExecutionBackend` factory. Its complete source is intended to be copied into a
protected deployment directory and pinned by the executor runtime's
`factory_sha256`. The module includes its own no-redirect, one-request isolated
transport and response validators; it does not import an unpinned model SDK or
select a model provider.

The backend sends `POST` to one configured HTTPS endpoint. Loopback HTTP exists
only for explicit tests. Authentication is a bearer header loaded from an
owner-only file whose SHA-256 is bound by protected configuration. Neither the
credential nor its digest enters the JSON body or model input. The request
contains:

- schema, operation and exact backend/facility identities and versions;
- the complete host-owned execution assignment and upstream execute digest;
- explicit `inherit_context: false`, empty tools and zero delegation depth;
- for `execute` only, the already reduced phase task and all bounded budgets;
- a canonical request SHA-256 echoed in headers and the response.

`Idempotency-Key` is exactly the host execution key. The remote facility must
atomically persist that key and canonical execute request before physical model
work, reject a changed request under the same key with HTTP 409, and keep the
completed execution plus usage available to read-only `reconcile`. The client
does not retry. A lost execute response therefore becomes executor state
`unknown`; only reconciliation may recover it. `not_started` is accepted only
from reconciliation and must never represent a failed or unobserved start.

Responses use
`translate-native.subagent-review-facility-response.v1`, repeat the operation,
backend and facility generations, execution key, upstream execute digest and
request digest, and return exactly one existing executor status. HTTP 202 is
required for `running`, `unknown` and `cancel_pending`; HTTP 200 is required for
`completed` and reconciled `not_started`. Only `completed` carries execution and
usage. Reviewer, session, phase, model, empty-context flags, cost, input bytes,
output tokens and original execute digest are checked by both this adapter and
the durable executor before evidence can reach the trusted review host.

Use `integrations/subagent-review-backend.example.json` with the executor
example. Replace all placeholder paths, revisions and SHA-256 values, place the
token and both configuration files in an owner-only directory, and keep the
facility's provider credential outside this adapter. The facility remains
responsible for mapping the pinned model identity to a supported host subagent,
real billing, cancellation and qualified-reviewer evidence. Missing support or
unverifiable execution remains blocked rather than being synthesized locally.

### Durable host-subagent facility

`integrations/website_localization_subagent_facility.py` is the server half of
the standard HTTPS backend. It authenticates before reading the body and checks
the closed request schema, both canonical digests, header bindings, exact
facility generation, reviewer assignment, route, budgets and phase-specific
model input. The target-native route rejects source content and inherited
conversation metadata again at this boundary.

Before invoking a host driver, the facility atomically stores the complete
binding and a `dispatching` barrier in SQLite. The provider execution key is
stable for that exact assignment and upstream execute digest. An identical
execute replays stored state; a changed request under the same key returns a
conflict. A lost reply is recovered through `reconcile`, which is forbidden from
starting work. Ambiguous, running and cancellation-pending work continues to
consume capacity. A runtime restart fences an earlier boot's unfinished
dispatch as `unknown` instead of assuming that it never started.

The provider-neutral driver implements only `execute_idempotent` and
`reconcile`, and must declare atomic idempotency, read-only reconciliation, hard
deadline enforcement and isolated-context support. Its trusted result supplies
the actual reviewer, session, phase, model and bounded usage. The facility
verifies all fields and constructs the executor-facing evidence itself. It does
not give the driver a Guard signer, publication authority, facility credential,
route table or journal.

The protected runtime executes each driver operation in a fresh process and
kills the complete process group at the assignment deadline. It never retries a
start. Provider work accepted before a killed response remains recoverable only
under the same provider execution key. A single owner-only deployment lock is
held before crash fencing and until shutdown, so another runtime or `--check`
cannot mutate a live dispatch owner's journal. Each child inherits that lock
and enforces the same deadline itself; abrupt parent death therefore cannot
release crash fencing while a driver child remains able to start. The child uses
Python isolated/no-site mode, accepts only the startup-pinned driver settings
digest, and returns only a structured content-free error classification. This
hardened implementation requires POSIX descriptor and process-group semantics;
it blocks startup on unsupported operating systems.

Copy `integrations/subagent-review-facility.example.json` and
`integrations/subagent-review-driver.example.json` to an owner-only absolute
directory. Pin the exact driver factory bytes and initialize once:

```console
python integrations/website_localization_subagent_facility_runtime.py \
  --config /absolute/protected/path/subagent-facility.json \
  --initialize-ledger --check
```

Normal starts omit `--initialize-ledger`. Missing, replaced, permission-weakened
or deployment-drifted state blocks before readiness. The bundled server listens
only on loopback behind a trusted HTTPS terminator. The driver settings may hold
references to operator-managed provider credentials, but those credentials must
never enter task input or review evidence. A driver without genuine host
subagent support must block rather than synthesize an agent review.

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
network access, so source fields and extra metadata fail closed. This also
applies to the ordinary-response schema, which has no source-aware phase.

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
`tests/test_response_subagent_review.py`, `tests/test_guard_service.py`, the
Claude hook suite and the HTTPS bridge suite cover the ordinary-response path,
including source isolation, self-review, false identities/phases/locales,
replay, edited candidates, missing hosts, low confidence and timeout. The tests
use synthetic Finnish and Maltese text plus fixture host ledgers. These fixtures
test protocol behavior, **not** linguistic acceptance, concrete host sandboxing
or superiority over DeepL. A deployment must implement and verify the host
contract above before enabling either provider identity. No live host
configuration is installed by this change.
