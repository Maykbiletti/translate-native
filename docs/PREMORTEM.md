# Version 6 premortem

## Public website capability discovery (13 September 2026)

Assume the in-process website runtime had the correct end-to-end capability,
but a public discovery boundary exposed a weaker or private variant.

- A body, query, insecure transport, or wrong scope could reach the runtime.
- Authentication could occur after the runtime had already read its database
  or contacted the downstream sidecar.
- A replaced runtime could add an endpoint, tenant, credential, or website
  content under a nested field and recompute the outer hash.
- A plausible but stale schema or changed publication semantic could be served
  as if it were the active contract.

The read-only WSGI adapter now requires exact HTTPS GET semantics and validates
the host-supplied principal before any runtime access. It accepts only the
closed V6.96 capability shape, exact nested operations and semantics, bounded
retry policy, verified durable binding, and matching canonical digest. Tests
prove authentication-before-runtime ordering, reject every alternate request
shape and substituted contract, and require stable content-free failures.

## Authenticated source runtime binding (12 September 2026)

Assume a sidecar reached a healthy source API whose static HTTP contract was
correct, but whose durable worker used another price and offer capability
generation.

- A static API digest could remain unchanged while the internal commercial
  profile or locale-rendering registry changed.
- A capability check followed by a write could lose the verified runtime
  binding at the response boundary, leaving the caller unable to prove which
  generation accepted the work.
- Health and readiness could report a plausible service state without exposing
  whether all three durable databases still carried the same verified binding.
- A sidecar could accidentally discard or reshape the binding while forwarding
  operational evidence, hiding a partial deployment mismatch from the website
  host.

The source HTTP application now requires a verified durable binding before it
can be hosted, returns the exact content-free binding with capabilities and
every operation, and revalidates it after runtime work. The contract-pinned
source client requires both internal deployment pins and rejects missing,
stale, malformed, or substituted bindings. Source delivery independently
revalidates and forwards the same binding through health and readiness, so a
single mismatch blocks fail-closed rather than being reduced to a healthy
aggregate state.

## Locale-exact commercial rendering references (12 September 2026)

Assume every commercial provider phase received the correct language profile,
but rendered amounts, percentages, ranges, and currency labels with conventions
from a different EU locale.

- A language name alone cannot distinguish Austrian grouping or Portuguese
  spacing from a generic parent-language default.
- Treating one punctuation spelling as semantic proof would reject equivalent
  number words, native digits, or safely reformatted values.
- An unversioned external reference could change without invalidating queued
  jobs, cached targets, review evidence, or publication approvals.
- Copying a formatting table by hand could silently transpose two locales or
  normalize non-breaking spaces into ordinary spaces.

Embed a canonical CLDR 48 number-format reference in each of the 24 commercial
locale profiles, including the resolved CLDR locale, numbering system, decimal
and grouping symbols, minimum grouping threshold, decimal, percent, currency,
ISO-currency, approximation, limit, and range patterns. Bind the complete
reference and its official tagged source URL into the profile digest and every
provider phase. Treat these values as rendering guidance only: semantic review
must accept meaning-preserving surface variants and route ambiguous values to
independent model or qualified native-domain review. Tests cover all locales,
exact Maltese and Finnish conventions, Austrian and Portuguese region overrides,
Unicode spacing, profile tampering, and the ban on deterministic regex proof.

## Commercial escalation evidence at publication (12 September 2026)

Assume a locale reached the CMS with a commercial review summary that still
said `review_required`, while the publication evidence did not say whether the
targeted dimensions were resolved by an independent model or a qualified human.

- A valid approval could conceal which escalation path actually satisfied the
  unresolved amount, tax, renewal, cancellation, or condition checks.
- A receiver that accepts `review_required` without a matching resolution could
  mistake an unresolved primary review for publishable evidence.
- A generic receipt hash could be copied to a different ordered dimension scope
  unless the scope and review method remain inside the signed publication.
- Exposing raw receipts or reviewer prose would leak sensitive review material.

Advance the publication-evidence contract, add one content-free resolution only
when the commercial summary is unresolved, and bind its exact ordered dimensions,
review method, receipt hash, and independent provider identity when applicable.
Require the receiver to reject missing, unexpected, cross-scope, malformed, or
method-inconsistent resolution evidence before its commit callback. Prove both
allowed escalation paths and keep verified commercial and non-commercial traffic
compact with a `null` resolution.

## Commercial evidence HTTP profile binding (12 September 2026)

Assume a valid commercial worker result could not reach the remote quality
service, or a remote receipt was accepted without the exact locale-specific
commercial profile that produced it.

- The worker's nested commercial profile could be rejected by an older HTTP
  validator that only permits the three base locale-profile fields.
- Accepting an optional nested object would let commercial requests omit it or
  let non-commercial requests smuggle unrelated commercial scope.
- Checking only token and digest shapes would allow a different commercial
  profile identifier to travel beside an otherwise valid review summary.
- Keeping the old evidence and receipt schema generations would let durable
  retries silently reuse pre-binding requests or receipts.

Version both contracts, require the exact compact commercial profile binding
inside `quality_profile` only when `content_type` is `commercial`, and require
its profile identifier to match the top-level commercial policy and review
summary before any authentication or network access. End-to-end tests carry a
real commercial coordinator request through both HTTPS adapters and prove that
missing, extra, stale-shaped, or cross-profile bindings fail closed locally.

## Locale-bound commercial publication evidence (12 September 2026)

Assume a CMS replaced its current localized offer with a signed bundle whose
generic commercial profile was correct but whose locale-specific profile was
stale or belonged to another EU language.

- A generic profile ID cannot prove that Finnish, Maltese, or another locale
  used the intended native-language commercial quality generation.
- Adding an optional digest would let old senders silently bypass the binding.
- Comparing only well-formed tokens and hashes would accept a valid-looking but
  obsolete or cross-locale profile.
- Applying commercial requirements to ordinary content would create false
  publication blocks.

Version the release-evidence schema, require a compact commercial quality
binding only for commercial content, and derive it from the already approved
worker result. The CMS receiver recomputes the canonical binding for each exact
locale before its commit callback; the durable store independently rejects
missing or structurally inconsistent bindings during its atomic recheck. Tests
cover all 24 EU locales, non-commercial compatibility, malformed evidence, and
stale version, digest, and profile substitution before commit.

## Locale-bound commercial quality profiles (12 September 2026)

Assume a commercial job advertised one of 24 EU locales while every provider
phase still received the same universal pricing prompt.

- A locale label without locale-specific evaluation guidance could accept
  source-shaped price labels, interval wording, CTAs or contract terms.
- A profile omitted from the job digest could change without invalidating cache
  entries, review evidence or publication authority.
- A caller-controlled profile could weaken one difficult language while still
  presenting a valid generic commercial profile identifier.
- Passing the source into the target-only phase through profile data would
  destroy the required independence of the native-language review.

Derive one canonical commercial profile from each existing locale quality
generation, bind the generic commercial policy plus locale profile version and
digest, and hash the complete object into the job identity. Recompute it before
provider access, pass the content-free profile to all three ordered phases, and
retain its version and digest in signed release evidence. Tests cover all 24
locales, Maltese and Finnish risk markers, phase separation, profile drift,
mutation before provider access, cache invalidation and release tampering.

## End-to-end processing health (12 September 2026)

Assume every intake worker appeared healthy while the source processing queues
were blocked or damaged.

- A healthy website outbox and sidecar could hide exhausted source retries,
  corrupt queue counters, or incomplete terminal processing.
- One aggregate health flag could erase which operated boundary is degraded or
  blocked.
- Continuing to the source layer after an earlier intake failure could create
  misleading secondary network errors.
- A structurally valid health response from another capability generation could
  be accepted after configuration drift.

The sidecar exposes a distinct authenticated, body-free source-health operation
and validates the complete source response against its pinned contract. The
website runtime first validates website and sidecar intake health, stops before
the source call when intake is blocked, preserves both projections and their
error details separately, binds both capability hashes, and fails closed on
every transport, schema, status, or contract inconsistency.

## End-to-end processing readiness (12 September 2026)

Assume a website accepted localization work while the source service could not
actually process it.

- A healthy website worker and sidecar could hide a stopped or blocked source
  worker.
- Combining all readiness data into one object could erase which operated
  boundary is unavailable.
- A local failure could still trigger an unnecessary downstream request and
  create misleading network noise.
- A valid source readiness response from another capability generation could
  be accepted after configuration drift.

The sidecar exposes a distinct authenticated, body-free source-readiness
operation and validates the complete source response against its pinned
contract. The website runtime first verifies local and sidecar intake; it
contacts the source layer only when both are ready, preserves both readiness
objects separately, binds both capability hashes, and fails closed on every
transport, schema, status, or contract inconsistency.

## End-to-end localization lifecycle status (12 September 2026)

Assume a website followed a durably accepted submission into localization but
read the wrong tenant, an event that had not reached the source service, or an
obsolete status contract.

- A source-status request before sidecar acceptance could probe or invent
  downstream state for work still owned by an upstream outbox.
- A valid response for another site, event, payload, or capability generation
  could be attached to the local submission.
- Collapsing source acceptance and the localization lifecycle into one success
  could make processing, review, or publication appear complete too early.
- Reformatting the rich source status at each hop could silently omit a failed
  locale, retry counter, terminal notification, or receiver state.

The sidecar exposes a distinct authenticated source-status operation only for
an exact locally succeeded outbox row. Every hop binds the site, event, stored
payload hash, sidecar capability hash, and source-service capability hash. The
website runtime keeps acceptance and localization as separate nested status
objects, validates the complete source payload without lossy normalization,
and fails closed on missing, premature, malformed, or stale state.

## End-to-end submission health (12 September 2026)

Assume one durable submission outbox appeared healthy while the other was
blocked, corrupt, or bound to a changed sidecar contract.

- A website-only health check could stay green while sidecar delivery is
  permanently failed or holding an expired lease.
- A healthy sidecar could hide failed or stale work in the website outbox if a
  deployment reduced both states to one averaged signal.
- Malformed counters or a substituted capability hash could be normalized into
  a plausible healthy response.
- Probing the sidecar before validating local storage could send an
  authenticated request from a runtime whose own state cannot be trusted.

The combined projection validates the local health snapshot before network
access, then uses the owned authenticated client and requires the exact
sidecar schema and capability binding. It preserves both complete content-free
snapshots and reports `ok` only when both independently report `ok`.

## End-to-end submission readiness (12 September 2026)

Assume the website worker appeared healthy while the authenticated sidecar
worker was stopped, blocked, or reporting an incompatible state.

- A local-only readiness check could allow an operator to declare the complete
  submission path available while accepted work cannot advance downstream.
- Querying the sidecar when the website worker is already unavailable could
  waste credentials and present an irrelevant remote success as overall health.
- Averaging two states could let one healthy outbox conceal the other boundary's
  blocking error.
- A malformed readiness response or changed capability contract could be
  normalized into a positive result.

The combined projection validates local readiness first and remains offline
unless that boundary is ready. It then uses the owned authenticated client,
requires the exact response schema and capability binding, preserves both
worker and outbox states independently, and reports ready only when both
durable intake workers are running with healthy outboxes.

## Bound submission progress (12 September 2026)

Assume an operator polled a successfully queued website event and mistook one
durable boundary for completion at the next boundary.

- Reading the sidecar before local acceptance could disclose or manufacture a
  remote state for work that the website still owns.
- A status response for another event, tenant, payload, or contract generation
  could be attached to the local row.
- Collapsing the three retry ceilings could hide which queue exhausted its
  attempts and make an unsafe retry appear available.
- Sidecar-to-source acceptance could be reported as finished localization or
  publication even though those later stages have not run.

The combined status projection stays local until website acceptance, then uses
the owned authenticated client and revalidates all immutable identities,
payload and capability hashes, and both downstream retry ceilings. It exposes
three explicit acceptance stages, carries only content-free error codes, and
defines final `accepted` as durable source-service acceptance rather than
localization completion.

## Owned authenticated submission runtime (12 September 2026)

Assume a website operator assembled the valid HMAC client, sidecar adapter,
SQLite outbox, and worker separately but still lost a source event or confused
durable acceptance with downstream completion.

- The rotating client could omit the public immutable contract needed by the
  outbox adapter and fail only when production wiring starts.
- A credential replacement could update a signer other than the one used by
  the durable worker.
- Invalid worker or middle-retry configuration could create persistent state
  before failing, or trigger an early network request.
- A local succeeded row could be presented as completed source processing even
  though the sidecar still reports pending or failed work.

The owned submission runtime constructs one rotating signer, pinned client,
adapter, guarded outbox, and optional worker from one configuration. It exposes
the client's immutable contract to the adapter, validates hosted delays before
opening SQLite, routes rotation through the owned client, and gives local
acceptance and authenticated sidecar lifecycle reads separate method names.
Process and close guards run before either storage or transport access.

## Durable authenticated sidecar submission (12 September 2026)

Assume a website persisted an event but mixed the retry policy for reaching the
sidecar with the sidecar's delivery policy or the source service's processing
policy.

- A transient sidecar outage could exhaust the downstream source retry budget.
- A changed sidecar pin or inner retry ceiling could alter an already queued
  event after restart.
- Treating sidecar acceptance as a direct source-service response could let a
  malformed acknowledgement satisfy the existing outbox validator.
- Translating a client exception could accidentally turn an unknown failure
  into a retryable one.

The sidecar outbox adapter gives each boundary its own explicit retry ceiling
and hashes both capability pins plus the inner delivery policy into the
outbox's immutable contract binding. It accepts only the exact validated
sidecar envelope before producing a private completion projection, preserves
declared retryability, and makes every unknown or malformed failure permanent
and content-free.

## Source-bound commercial review evidence (12 September 2026)

Assume a structurally valid, content-free commercial review summary was copied
to a different source, target, or policy generation.

- A digest covering only the evidence object could remain unchanged when the
  reviewed source or candidate changed outside an evidence span.
- Updating the digest recipe without the commercial profile could leave old
  plan, cache, and approval identities apparently current.
- A worker and authenticated capability registry could enforce different hash
  recipes while sharing the same summary schema.
- Mutation tests could cover changed reviewer evidence but miss exact Unicode
  text or profile changes.

Commercial profile v3 and summary-contract v2 now hash a versioned binding of
the exact UTF-8 source hash, exact UTF-8 target hash, profile, and complete
canonical evidence. The profile bump invalidates earlier derived identities;
the capability registry validates and publishes the same recipe. Independent
tests mutate source, target, profile, and evidence and require distinct digest
changes for every input.

## Single-source authenticated client composition (12 September 2026)

Assume a website backend wired a valid rotating signer to a valid HTTPS client,
but duplicated their security configuration incorrectly.

- The signer and client could receive different origins or capability pins and
  fail only after the service entered production.
- An adapter could rotate one signer while a different signer still protected
  outgoing operations.
- A convenience wrapper could omit status, health, readiness, or removal and
  encourage an unprotected alternate path.
- A forked worker could reach the network through an inherited client before
  the signer rejected its first proof.

The composed client now accepts origin and both capability pins exactly once,
constructs its private rotating signer and HTTP client together, and exposes
all six contract operations through one process-bound surface. Configuration
failure performs no network call. Credential replacement reaches the exact
signer used by every operation and retains its previous credential on failure.

## Uninterrupted source-delivery client rotation (12 September 2026)

Assume the server accepted an overlap generation, but a long-lived source
worker could not move its HMAC signer safely without a restart.

- Concurrent requests could observe a partially replaced credential or change
  generation halfway through proof creation.
- Invalid secret-manager output could destroy the last working client signer.
- A forked process could inherit signer state and silently reuse parent-owned
  secret material and synchronization.
- Retiring the old server generation immediately after one client swap could
  reject proofs already emitted or leave other client instances behind.

The client now owns one process-bound rotating signer. It validates a complete
replacement under the same lock used for proof creation and swaps only after
success, preserving the previous signer on failure. Rotation never changes the
fixed origin or capability pins. Deployments overlap generations server-side,
rotate every client instance, drain already emitted requests and the proof
validity window, and only then retire the old generation.

## Uninterrupted source-delivery credential rotation (12 September 2026)

Assume a running source-delivery sidecar rotated credentials but briefly
accepted an incomplete set, lost its last working verifier, or weakened replay
protection.

- An in-flight request could race the replacement and observe a partially
  updated credential map.
- Invalid secret-manager output could remove every valid generation before the
  configuration error became visible.
- Removing and later re-adding a generation could make an already consumed
  proof usable again if rotation also reset replay state.
- A failed update could bypass storage, process, or lifecycle guards, while its
  private exception or secret value escaped through a stable error.

The runtime now materializes and validates one complete replacement verifier,
then swaps it atomically under the same lock used by authentication. The prior
verifier remains active until the replacement and replay-path guard both pass.
All generations share the unchanged durable nonce ledger, so retirement and
re-enrollment do not restore consumed proofs. Closed, inherited, replaced, or
permission-weakened runtimes reject the update; failures are reduced to stable
content-free codes.

## Durable terminal-processing reconciliation (11 September 2026)

Assume the source CMS received a valid intake acknowledgement but later lost
the actual CMS processing result.

- An accepted notification could be reported as complete while its receiver
  processing row was still pending or had failed terminally.
- A status response for another event, site, terminal outcome, or payload could
  be attached to the local delivery.
- Two source workers could poll the same observation concurrently, or a crash
  could strand a local status lease indefinitely.
- Repeated network failures could keep the source apparently healthy forever,
  while private transport detail leaked into durable state.

The source now registers an independent processing observation only after the
exact notification acknowledgement commits. Its SQLite ledger binds the
notification, event, site, terminal status, and payload SHA-256; uses expiring
leases and bounded content-free retry codes; and accepts only the receiver's
fully bound status shape. Pending work remains visible, while remote failure,
exhausted observation, lease expiry, response drift, or storage corruption
blocks source health fail-closed.

## Response-bound terminal-receiver contract (11 September 2026)

Assume capability discovery passed but the subsequent operational response came
from a stale, switched, or differently configured receiver.

- A load balancer could route discovery and health to deployments with different
  notification paths or contract versions.
- A cached health response could predate a contract change while still matching
  the expected health schema.
- A status response could omit the contract identity, leaving the client unable
  to prove that its event and site were read under the pinned interface.

Every operational response now carries the live canonical capability SHA-256,
and the advertised response field sets include that binding. The client checks
it against its trusted pin after the fresh discovery preflight; missing, stale,
or mismatched bindings block rather than accepting ambiguous evidence.

## Contract-pinned terminal-receiver operator client (11 September 2026)

Assume an operator accepted a terminal-receiver control response from the wrong,
changed, or semantically weakened endpoint.

- A redirect could carry credentials to another origin, or a transport wrapper
  could silently repeat a supposedly read-only request.
- A validly rehashed contract could reuse write authority for health, change a
  route or schema, or advertise a partial interface under the expected product.
- Status could be returned for another event or site, while inconsistent health
  counts or a truncated response could still be labeled healthy.
- A deployment could pin the contract once but omit checking whether it changed
  before a later operational read.

The new operator client requires an out-of-band capability digest and verifies
the live canonical contract, exact operation semantics, distinct scopes, and
schemas before every health, readiness, or status request. Requests bind fresh
authentication to method, exact origin, path, body hash, event, and site; the
transport performs one bounded attempt without redirects. Exact response fields
and cross-field invariants distinguish valid blocked state from malformed
evidence, while every local failure exposes only a stable content-free code.

## Terminal-receiver operational health API (11 September 2026)

Assume the hosted terminal receiver accepted work while its operational health
report was stale, misleading, state-changing, or exposed tenant information.

- Readiness alone could hide due work, expired leases, or permanently failed
  processing records from an operator.
- A stopped or failed managed worker could be reported healthy because SQLite
  integrity still passed independently.
- A health request could reuse a write credential, mutate retry state, or expose
  event, site, notification, target text, or private callback details.
- The runtime route and machine-readable capability contract could drift or a
  custom intake path could collide with the health path.

The new body-free health route uses its own scope and authenticates before a
read-only aggregate inspection. It combines runtime, worker, SQLite integrity,
processing counts, due work, expired leases, and terminal failures; incomplete
evidence, worker failure, and storage failure return content-free `503` with a
stable code. The canonical capability digest covers the exact route and fields,
while preflight rejects path collisions and scope reuse before SQLite opens.

## Durable terminal-notification processing (11 September 2026)

Assume the CMS accepted and stored a verified terminal notification but never
applied it safely to its own local state.

- A crash between the HTTP acknowledgement and processing registration could
  leave a durable receipt that no worker can discover.
- Two CMS workers could process the same notification concurrently, or an old
  worker could finish after its lease expired and a replacement took over.
- A callback could fail forever, persist private exception prose, or return an
  acknowledgement for a different event or payload.
- Migrating a V6.51/V6.52 inbox could backfill altered evidence or partially
  upgrade the schema before failure.

The receive transaction now inserts both the immutable inbox row and its
processing record before acknowledging. Claims bind worker, random token,
attempt, deadline, notification identity, and exact payload hash; completion
rechecks every field, and expired ownership cannot finish. Retry state and a
bounded attempt ceiling are durable, while only validated stable error codes
are stored. The V1-to-V2 migration validates the old schema and every receipt
inside one transaction before backfill, so any discrepancy rolls back without
partial state. Host side effects remain idempotent by notification ID because
an acknowledgement lost after the side effect must be replayed safely.

## Protected terminal-receiver runtime (11 September 2026)

Assume the durable callback receiver validated every request correctly but wrote
its evidence through an unsafe or stale SQLite connection.

- A symlink, hard link, permissive file, shared writable directory, or exchanged
  path could redirect or expose the terminal-notification ledger.
- A runtime constructed before a worker fork could reuse a connection and lock
  whose process ownership is no longer valid.
- Shutdown could close SQLite during a request, while a damaged database could
  continue returning superficially healthy counts.
- Invalid origin, route, authenticator, or HTTPS policy could be discovered only
  after an unwanted database file had already been created.

The composition root preflights the complete HTTP configuration before touching
the database, reserves a missing regular file exclusively with mode `0600`, and
pins its device/inode identity. Parent ownership, permissions, link count, file
mode, and identity are rechecked under one process-owned runtime lock before
every request, status read, and health check. Close shares that lock and is
idempotent. Health verifies SQLite integrity plus every stored semantic binding;
configuration, path, process, lifecycle, and storage failures remain fail closed.

## Durable terminal-notification receiver (11 September 2026)

Assume the source localization service reported a verified terminal result but
the website CMS recorded it twice, acknowledged it too early, or accepted it for
the wrong tenant.

- The CMS could commit a notification and lose its response, then repeat a host
  side effect when the durable sender replays the same bytes.
- A reused event or notification ID could carry a changed terminal state, source
  generation, lifecycle binding, or payload hash.
- Generic authentication could validate a credential without binding the exact
  method, origin, path, site, event, notification, and request bytes.
- The receiver could acknowledge before its local transaction commits, or expose
  verifier, database, or credential detail when a dependency fails.

The reference receiver requires canonical request bytes and exact reserved
headers, gives a host verifier the sender's complete content-free authentication
context, and requires a principal scoped to the same site. A serialized,
process-bound SQLite transaction stores one immutable notification before the
acknowledgement is constructed. Exact replays preserve the first receipt;
identity collisions, altered rows or schemas, invalid framing, and unavailable
authentication or storage fail closed through stable content-free responses.

## Durable source-side removal outbox (10 September 2026)

Assume a website backend removed content but an unpublished localization kept
running or an acknowledged publication remained in the CMS.

- A cancellation or tombstone could be accepted remotely just before the
  source process stopped, leaving the local sender uncertain and unscheduled.
- Cancellation and published-content deletion could be conflated even though
  they have different identities, eligibility rules, and acknowledgements.
- Concurrent source workers could both claim one removal, or a caller could
  reuse an ID with changed source-generation bindings.
- A transient transport failure could retry forever, while source identifiers
  or private exception detail could escape through operational status.

The shared removal outbox stores canonical cancellation and tombstone bytes
before dispatch while retaining their separate operation and request IDs. A
transactional lease permits one exact client method call per attempt; expired
leases replay the same bytes against the server's idempotency ledger. Changed
bindings collide, retryable failures use capped backoff and a fixed attempt
ceiling, and permanent failures stop. Status and health expose only stable
identifiers, counts, codes, times, and hashes. Payload, schema, response, or
lease inconsistencies block before another network call.

## Durable source-side change outbox (10 September 2026)

Assume a website backend detected changed content but the localization service
never received it exactly and durably.

- A process could stop after remote acceptance but before recording success,
  or before a volatile retry was scheduled.
- Two source workers could send different bytes under one event identity or
  concurrently treat one due event as theirs.
- A temporary transport failure could loop without a bound, while a permanent
  contract failure could be retried indefinitely.
- Source text or private exception detail could escape through status, health,
  or worker logs.

The source-side change dispatcher persists canonical native-Unicode event bytes
and their SHA-256 before dispatch, rejects identity collisions, and leases one
due event transactionally across SQLite connections. Each lease invokes the
secure HTTP client once. Retryable failures use capped exponential backoff and
the stored attempt ceiling; permanent failures stop. Expired leases replay the
same immutable event, so a crash after remote acceptance converges through the
server's event idempotency. Status, health, and outcomes contain only IDs,
counts, stable codes, timestamps, and hashes; payload or database tampering
blocks before a network call.

## Source-side CMS HTTPS client (10 September 2026)

Assume a website backend implemented the documented six-operation API but
still leaked a signed request or accepted another event's state.

- A general HTTP library could follow a redirect and forward host credentials
  plus signed source content to a different authority.
- Library-level retries could repeat a mutation outside the CMS event's durable
  retry and idempotency policy.
- A syntactically valid response could carry another request ID, event, site,
  or a substituted capability contract.
- Signing mutable caller data could let the transmitted bytes diverge from the
  event the CMS intended to submit.

The source-side reference client now accepts one fixed HTTPS origin, refuses
credentials in URLs and redirects, performs exactly one transport attempt, and
leaves retry scheduling to the host. It first copies each request into canonical
native-Unicode JSON, signs those exact bytes, and binds every response to the
operation plus request, event, site, and request ID. Capability and API-contract
hashes are recomputed, while the exact six ordered operation definitions are
checked independently so rehashing a substituted route cannot make it valid.
Authentication, parser, transport, server, and binding failures remain
content-free with an explicit retry decision.

## Generation-bound CMS rendering (10 September 2026)

Assume the CMS safely retained its last-known-good localization while a new
source generation was being processed, but rendered that old bundle as though
it belonged to the new page or policy.

- Looking up by only site and source ID cannot distinguish the previous source
  revision from the newly registered one.
- A glossary, policy, locale set, content type, commercial profile, or website
  version change could therefore inherit target prose approved for another
  binding.
- Removing the old bundle immediately would also be wrong: a failed replacement
  must not destroy the last-known-good website version.

Trusted rendering now supplies the complete publication expectation used for
that page generation. The store validates its source hash, sequence, revision,
event, plan, website version, exact locale set, content type, and commercial
profile against the active signed payload before returning any target text. A
new expectation cannot read the retained old bundle, while the explicitly old
expectation can still render it until a complete replacement commits. After
the atomic switch, the old expectation fails closed and its superseded prose is
already scrubbed.

## Durable CMS database path confinement (10 September 2026)

Assume a correctly signed localization was exposed or written to an unintended
SQLite file even though callback validation remained correct.

- A deployment path could traverse a symlink or a shared writable directory,
  letting another account substitute the database before it opens.
- A pre-existing permissive file or a hard link could expose approved target
  prose outside the CMS receiver's intended storage boundary.
- Permissions or the path identity could change after startup while health and
  publication continued to report success.
- Two workers starting together could race creation and leave inconsistent
  access modes.

The composition root now accepts only canonical absolute POSIX paths beneath a
real service-owned parent whose ancestors are also trusted and non-shared-
writable, except for root-owned sticky temporary directories. It reserves a
missing file with exclusive creation and mode `0600`, or verifies the same
ownership, mode,
regular-file type, and single-link invariant on an existing database. Every
store path rechecks those properties and the original device/inode identity
under the worker lock. Concurrent creators converge by validating the winning
file; symlink, hard-link, replacement, ownership, permission, or parent drift
blocks before SQLite work and returns no customer text.

Assume the Version 6 response-and-translation gateway and automatic updater shipped and failed in production.

| Failure | Early warning | Mitigation | Proof required |
| --- | --- | --- | --- |
| Signed commercial benchmark evidence preserves only totals, so an operator cannot see which offer dimension failed | The final report blocks, but amount, tax, renewal, cancellation, and conditions remain indistinguishable without reopening private reviewer output | Unblind only the four content-free dimension statuses into each signed case, bind them to the exact source-fidelity response hash, aggregate every dimension separately, and block a dimension on any candidate major/blocking status | Case and report tests prove exact A/B-to-origin rebinding, ten ordered dimension summaries, per-status counts, no reviewer prose, a visible candidate-defect block, and rejection of a re-signed response-hash mismatch |
| A commercial benchmark reviewer receives all ten dimensions but returns only a generic preference | The request looks complete while tax, renewal, cancellation, or target-only inventions were never explicitly decided; uncertainty is averaged into a win | Require an ordered per-dimension acknowledgement for both anonymous variants, link major/blocking decisions to exact defect entries, reject uncertainty, and enforce the same conditional contract in the runner, HTTPS adapter, and durable store | Positive end-to-end and HTTPS/store tests cover ten decisions per variant; missing, additional, reordered, uncertain, unbound, and preferred-defect responses fail closed without becoming benchmark evidence |
| Commercial benchmark tags drift from the public offer profile or leak semantic source scope into the source-blind pass | A pricing case omits tax or cancellation review, accepts an invented target-only claim, or native review receives source-aware dimensions | Bind all ten ordered dimensions from the shared commercial registry to every commercial suite case, include the scope in the suite digest, validate it before any reviewer call, and expose it only during source-fidelity review | Manifest tests cover eight cases by all ten dimensions; missing, additional, and reordered scope block with zero reviewer calls; request tests prove native scope isolation and fidelity exposure |
| The public commercial capability drifts from the worker's enforced offer profile or leaks project-specific prices and brands | A CMS preflight accepts different dimensions than the source-fidelity review, or capability output contains customer catalog data | Generate a separately hashed, brand-neutral contract from the same versioned dimension registry and fail the full capability response closed on missing, reordered, renamed, or malformed rules | Signed capability tests assert all ten dimensions, exact amount/currency and term policies, locale-aware rendering, ambiguity escalation, hash integrity, absence of project data, and profile-drift rejection |
| A review adapter recognizes the commercial summary schema name but interprets its fields, status invariant, dimension order, or evidence hash differently | Targeted tax or cancellation review is lost, reordered, or approved against a partial digest even though discovery appeared compatible | Publish a separately hashed machine-readable summary contract from the same dimension registry, declare exact fields/statuses/hash semantics and excluded content, and fail the complete capability response closed on any drift | Direct and signed capability tests pin the five fields, two states, ten ordered unique dimensions, complete-evidence digest, sensitive-content exclusions, nested hash, and unknown/reordered contract rejection |
| Commercial uncertainty collapses into a generic second-review flag | A tax or cancellation ambiguity is detected, but the independent reviewer receives no exact review scope | Persist only an ordered, content-free dimension summary plus the full-evidence hash and bind it into the result, evidence request, and receipt | One-dimension and whole-coverage uncertainty retain exact routing; source values and reviewer prose are absent; malformed, reordered, substituted, or contradictory summaries block before transport or signing |
| A receipt survives edited text | Verification accepts a changed source, target, locale, or expired receipt | Sign canonical hashes, version, issue time, expiry, and nonce with HMAC-SHA256 | Tampering and expiry tests fail closed |
| Agents invent receipt-shaped strings | A token passes based on its prefix or format | Verify the cryptographic signature and exact payload; never trust appearance | Forged-token regression test |
| Legitimate RTL text is blocked | Balanced isolates in Arabic or Hebrew fail | Block overrides/embeddings; allow only balanced isolates; flag unpaired controls | Balanced/unbalanced RTL tests |
| Script validation becomes a language allowlist | Unknown languages fail automatically | Apply script checks only when a requested script is known; otherwise report `not-evaluated` | Unknown-language clean pass |
| Format support is marketing only | A translated file passes after keys, timestamps, or placeholders change | Implement format-specific structural parsers and negative fixtures | One positive and multiple mutation tests per format |
| Glossaries reject normal inflection | Native grammatical forms are flagged as missing | Support exact, case-insensitive, and optional regex terms; keep glossary mode explicit | Inflection/regex fixtures |
| Installer damages existing configuration | Existing CLI settings disappear | Never overwrite host configs; install symlinks atomically and emit mergeable snippets | Idempotent install test |
| Installer deletes a predictable staging path or overwrites a concurrently replaced target | A pre-existing `<target>.new` disappears, or a real file created after the initial check is replaced during command or skill installation | Reserve an unpredictable temporary symlink exclusively, preserve every colliding path, recheck the destination identity immediately before cutover, and remove only the installer-owned temporary link | Legacy and random staging collisions remain byte-identical; an exchanged non-symlink survives; idempotent symlink updates still pass |
| Installer follows or races a service-definition path before activating it | A linked systemd unit or LaunchAgent overwrites unrelated user data, a hard link mutates another file, or a concurrently exchanged definition is replaced before `systemctl` or `launchctl` runs | Accept only bounded single-link regular definitions owned by the user and not writable by another account; write owner-only temporary files atomically and recheck the exact destination identity immediately before replacement | Symlink, hard-link, FIFO, and exchange-race fixtures preserve their targets and abort before any service-manager command; valid Linux and macOS definitions retain their previous contents and scheduling behavior |
| Installer follows or races a service-definition parent directory | A linked `systemd` or `LaunchAgents` directory redirects an otherwise safe atomic write, or an exchanged parent makes the activated pathname differ from the directory that was written | Traverse every component below the user home through no-follow directory handles, require owner-controlled directories, and create and replace the definition relative to the held final handle before rechecking its pathname identity | Linked and broadly writable parent fixtures remain empty and block before service-manager calls; a parent exchanged during the write receives no redirected file and cannot activate the detached definition |
| Reset removes a substituted or unrelated service definition | A linked, hard-linked, special, marker-free, or concurrently exchanged systemd unit or LaunchAgent—or its parent directory—is followed while disabling updater, monitor, or MCP autostart | Open every directory component below the user home without following links, require owner control, preflight every definition through bounded stable-identity reads on the held final handle, require service-specific BLUN markers before invoking the service manager, and unlink only through that same handle after another identity check | Unsafe and unrelated paths and redirected parents remain byte-identical without service-manager calls; exchanged directories and files survive; managed Linux and macOS definitions still install and remove cleanly |
| Auto-update installs a broken release | Update stops after replacing only part of the installation | Fetch, test, and validate before atomic symlink switch; preserve previous revision | Failed-update rollback test |
| Failed update rollback deletes or overwrites a concurrently replaced runtime artifact | A newly absent MCP command, bearer token, or Claude configuration appears during activation, or an updater-created path is exchanged before cleanup | Capture the exact post-install identity of every MCP and Claude artifact; preflight all identities before cleanup; remove only paths that were absent before the update and still match; restore an existing Claude configuration through an unpredictable atomic temporary file | Command, header helper, token, and Claude-config exchange fixtures preserve operator-owned replacements; unchanged updater-owned artifacts still remove or restore cleanly; an integrated activation failure reports rollback cleanup failure without removing autostart |
| Runtime failure rollback moves a concurrently changed checkout back to the previous revision | An operator edits the activated checkout or commits follow-up work while service activation is still probing; a later `reset --keep` changes the visible branch history | Immediately before reset, require the checkout to remain clean and `HEAD` to equal the exact candidate activated by this update; verify the exact clean previous revision afterward; block artifact cleanup and runtime restarts when either binding fails | Dirty-worktree and new-commit activation-race fixtures preserve the candidate or descendant `HEAD`, operator bytes, and commit ancestry while reporting rollback failure and making no second restart attempt |
| Doctor reports false green | Files exist but MCP cannot initialize or issue/verify a receipt | Run live MCP initialize, tools/list, issue, verify, and tamper probes | Doctor integration test |
| Reviewer rubber-stamps its own output | Creation and judgment share the same context | Require target-only native review before source-aware fidelity review; record separate attestations | Contract test in SKILL.md |
| Agent simply skips the skill or MCP tool | Unreceipted text reaches a user | Intercept candidate output outside the agent and fail closed in the host adapter | End-to-end test proves raw output cannot escape |
| Agent steals the signing key | Agent and signer share one writable user or container | Run signer/gateway under a separate OS identity or remote service; deny agent filesystem and socket administration | Sandbox escape test cannot read key or replace executable |
| Signing-key creation or diagnostic inspection is redirected through an unsafe or exchanged parent directory | A linked or broadly writable trust directory causes installation to create the root signing secret outside the inspected location or makes `doctor` report a foreign key as safe | Open every parent component without following links, retain the final directory handle through creation and inspection, and recheck its path identity before accepting success | Linked and broadly writable parents block creation and inspection; a parent exchanged during either operation causes a fail-closed result |
| Signing-key storage or its parent directory is redirected, hard-linked, oversized, or raced during first creation | A linked key lets a known secret mint valid receipts, an unsafe or exchanged parent redirects runtime signing and verification, a hard link leaves a second mutation path to the root secret, a huge file consumes memory, or two starters silently replace one another's key | Open every parent component without following links, retain and recheck the final directory handle, keep read-only verification from creating missing path components, accept only bounded owner-only single-link regular files, use stable-identity reads that include the link count, and create the final key path once with exclusive mode `0600` | Signer, portable verifier, installed verifier, host delivery, doctor, and installer reject unsafe parents, symbolic links, hard links, and invalid key files; missing verifier paths remain absent; parent exchanges fail closed while foreign data remains untouched; repeated creation preserves the first key |
| Service authentication token or its parent directory is redirected, hard-linked, oversized, or raced | A known linked token reaches the isolated signer, an additional hard link leaves a second mutation path to the trust secret, an unsafe parent redirects creation or a later doctor check, an unbounded file consumes memory, or concurrent installation silently replaces the trust secret | Apply one bounded no-follow and stable-identity contract in every Python and JavaScript consumer, including owner-only POSIX access and exactly one hard link; bind the link count into every read, hold a no-follow directory handle throughout common Python runtime reads, every installer, Claude-hook, and BLUN Code adapter read, and creation, and reserve the final token path exclusively | Signer, MCP, mandatory delivery, Claude hook, BLUN Code adapter, installer probe, and doctor reject unsafe token files and parent directories; symbolic-link, hard-link, writable, exchanged-parent, exchange-and-restore, missing read-only path, and direct-reader fixtures fail closed; direct environment tokens stay compatible |
| Persistent HTTP MCP bearer token or its parent directory is redirected, hard-linked, oversized, or raced | A known linked bearer token reaches the loopback gateway, an additional hard link leaves a second mutation path to the trust secret, an unsafe parent redirects creation or a later probe, a special file blocks Claude reconnect, or concurrent installation swaps trust state | Require bounded owner-only single-link regular files in the gateway, dynamic header helper, installer probe, and doctor; bind the link count into stable-identity reads, hold a no-follow directory handle throughout every gateway startup read, installer read, creation, and dynamic reconnect-header read, and reserve the final token path exclusively | Symbolic-link, hard-link, size, permissions, identity-race, parent-link, parent-permission, parent-exchange, missing read-only path, direct-reader, and pre-network probes fail closed without altering redirected targets |
| Mandatory delivery policy or its parent directory is redirected, hard-linked, oversized, weakened, or exchanged during inspection | A substituted policy enables direct output, an additional hard link leaves a second mutation path to enforcement state, an unsafe parent redirects installation, diagnosis, host delivery, or Claude Stop enforcement, removes isolated verification, leaks another JSON file, or gives the installer and hook different rules | Require bounded owner-only single-link regular JSON, no-follow stable-identity reads that bind the link count, and strict fail-closed field types in every policy consumer; retain a descriptor-relative, component-by-component validated directory handle across installer preflight, temporary creation, replacement, post-install verification, read-only doctor checks, host delivery, and Claude-hook reads without creating missing paths | Host delivery, Claude hook, installer, and doctor reject symbolic links, hard links, size, permissions, invalid-field, and identity-race fixtures; installer, host, and Claude-hook parent-link, parent-permission, parent-exchange, intermediate exchange-and-restore, and missing-read-only probes leave redirected or absent targets untouched |
| Auto-updater becomes a supply-chain path | An unreviewed remote commit installs silently | Test candidate before merge, permit trusted commit-signature enforcement, use fast-forward only, run post-install tests, retain rollback revision | Rejected unsigned and broken-update fixtures |
| An unsigned update executes repository tests before signature rejection | The active revision stays unchanged, but an import-time marker or side effect appears during candidate validation | When signature policy is enabled, verify the exact cloned update or checked-out rollback commit before test discovery, metadata reads, or any repository-owned code | Unsigned forward and rollback fixtures are rejected while their import-time markers remain absent |
| A direct update silently drops a previously enabled signature policy | Scheduled updates reject unsigned commits, but `update` without a repeated flag accepts the same candidate; a post-rollback recovery also forgets the policy | Resolve active and rollback-paused policy at every update and rollback entry point; combine requirements monotonically and reject malformed or non-Boolean policy fail-closed | Flagless direct update still rejects unsigned code without importing it; paused `true` survives; string `"false"` never reaches the worker |
| Updater state is redirected, oversized, broadly readable, malformed, or exchanged during inspection | A linked file invents a rollback target, leaks another JSON document through `status`, bypasses a paused-update Boolean, or blocks the scheduler on a special file | Treat `update-state.json` as bounded owner-only regular state, open without following links, compare identity across the read, validate commit hashes and persisted field types, and block every reader before Git commands or candidate execution | Loader, identity-race, status, scheduled update, direct update, and rollback probes reject unsafe state without running commands or altering the linked target |
| Updater policy storage is redirected or replaced with a blocking special file | A predictable temporary link overwrites an unrelated file, or `status`/scheduled `run` follows a policy symlink or hangs on a FIFO | Open only bounded regular non-link policy files, validate the stored schema on every public path, and write through an unpredictable owner-only temporary file | Symlink, FIFO, oversize and invalid-field fixtures block without worker execution; a pre-created legacy `.tmp` link and its target remain untouched |
| Disabling automatic updates removes redirected or concurrently replaced policy state | Reset follows a link, deletes a replacement policy, or removes the scheduler before discovering unsafe state | Preflight active and rollback-paused policies, retain their exact identities, recheck both before scheduler mutation, and unlink only unchanged inspected files | Linked policy leaves scheduler and target untouched; a policy exchanged after preflight survives and disable reports a blocked result |
| Re-enabling automatic updates overwrites the signed-commit policy or deletes concurrent policy state | Changing only the schedule writes the CLI default `false`; restoring after rollback deletes the paused `true` policy or a replacement created during activation | Resolve the monotonic active and paused policy before mutation, bind active replacement and paused removal to their inspected identities, and require explicit disable before lowering enforcement | Interval-only enable and paused-policy restoration retain `true`; invalid, linked, and concurrently replaced policy bytes remain untouched; disable then enable can deliberately reset it |
| Scheduler claims success but never runs | Update state timestamp stops advancing | `doctor` checks scheduler and update state age; operational alert on stale state | Forced scheduled run updates timestamp |
| Bad update passes tests but breaks hosts | Unit tests pass while CLI adapter fails | Canary rollout, post-update doctor, automatic rollback, phased release channel | Canary failure prevents stable rollout |
| Short SEO copy bypasses the 200-character profile | Titles pass despite folded spelling | Classify title/meta/UI content, return `REVIEW_REQUIRED`, and bind independent review to receipt | Real short-copy regressions cannot pass without review |
| CRLF is mistaken for changed content | Windows files differ only in size and raw hash | Maintain canonical text identity alongside byte identity; normalize BOM/newlines/NFC only | CRLF/BOM match while mojibake still fails |
| Caller approves its own review | `short_text_reviewed=true` releases damaged spelling | Treat caller fields as metadata only; measurable findings are unconditional; external reviewer owns real approval | Damaged text stays blocked with `reviewed=true` and `content_type=prose` |
| Correct German `ss` is mistaken for `ß` folding | `wissen`, `dass`, or `interessiert` blocks | Exclude `ss` from density heuristics; require lexical and locale context | Correct `ss` corpus passes while `ae/oe/ue` attack still blocks |
| A translation preserves tags but loses most text | Truncated target reports `structure intact` | Measure total linguistic units, segment count, and aligned segment coverage with script-aware thresholds | 71% omission exits nonzero while full and CJK controls pass |
| The CLI catches an omission but mandatory MCP release does not | A fully attested truncated target receives a token | Load the same auto-detected volume primitive inside `release_translation`; never trust a caller format switch | 71% omission stays blocked with all seven attestations set to true |
| Repo tests pass but the installed MCP cannot import a dependency | Server path resolves to a nonexistent repository subdirectory | Resolve bundled modules relative to `__file__`; start a copied isolated installation in tests | `tools/list` succeeds from a temporary flat `scripts/` directory |
| JSON manifests pass repository tests but Claude's strict plugin validator rejects the package | A marketplace root uses `.` instead of `./`, or `skills` names a Markdown file instead of its containing directory | Follow Claude's strict relative-path grammar, bump the manifest version so caches update, and resolve every declared skill directory in repository tests | Marketplace source begins with `./`; every skill path is a directory containing `SKILL.md`; the installed updater still runs Claude's own strict validator before activation |
| An untranslated source is copied unchanged into the target | Volume is 100%, so quantity checks pass | Compare the complete canonicalized source and target before parsing subtrees; block identity from 200 characters | Full unchanged page blocks in CLI and MCP while short shared names pass |
| One value in an otherwise translated i18n file remains in the source language | Whole-file identity differs and volume remains plausible | Compare aligned human-language segments after structure extraction; align JSON by sorted key rather than property order | One unchanged sentence and copyright counterexamples block in CLI and MCP while `BLUN King` passes |
| A regression appears to block for the intended rule but its input path is wrong | Exit code is nonzero, but stderr says `cannot read file` | Assert the diagnostic code or message as well as the return code; return `2` for unreadable input and `1` for evaluated content findings | Missing-file fixture cannot satisfy an identity assertion |
| Unchanged HTML values are swapped to defeat positional comparison | Every aligned pair differs even though no value was translated | Match substantial canonical source segments against the complete target segment multiset; retain source DOM paths for diagnostics | Swapped `<p>` values block in CLI and mandatory MCP release |
| Any automatic copyright exception mistakes prose for a fixed legal line | Real names and title-cased prose overlap at every practical length and word count | Remove the exemption; block every unchanged copyright-marked segment and require explicit human handling for legitimate fixed lines | `Copyright © 2026 Important Notice` blocks in JSON, HTML, plain text, and mandatory MCP release |
| Agents use native spelling in translations but strip it from ordinary replies | Only `release_translation` is wired into the host | Route every final answer through `release_response`; keep translation on its stricter path | Damaged German and Swedish chat replies block while clean controls receive response receipts |
| A translation is mislabeled as a normal response | The caller chooses task kind or omits the source | Make task classification and source capture host-owned; block response requests carrying a source and translation requests without one | Mode-confusion tests fail closed in both directions |
| A response token authorizes a translation | Receipts bind only text and locale | Bind `purpose` into the signed receipt and verify it in the final output hook | Cross-purpose verification fails even for identical target text |
| Agents ignore the translation skill/plugin | The MCP connection exposes tools but no persistent policy | Inject mandatory instructions at MCP initialization, expose the `translate-native` prompt, install persistent agent rules, and still require the external gateway | MCP initialize, prompt discovery, and host interception tests all pass |
| Agents choose `auto`, `all`, or a false locale to evade spelling checks | Language is self-reported in the tool call | Require an exact host-supplied language tag for response release | `auto` and `all` block; wrong-locale attacks are covered at the trusted adapter boundary |
| Claude releases with `de-DE` while the host delivery route expects `de`, or uses the wrong release purpose | The agent sees the policy as prose but still controls the MCP arguments | When host policy variables are present, rewrite the exact language in `PreToolUse`, deny a purpose conflict, repeat policy at prompt boundaries, and independently recheck both fields in `PostToolUse` | Exact rewrite passes; wrong purpose, malformed policy, and post-tool mismatch block; absent policy preserves compatibility |
| Agent prints raw prose instead of a release envelope | Wrapper forwards stdout before parsing it | Buffer stdout, require one strict JSON envelope, and emit no candidate bytes until receipt verification passes | Raw, malformed, oversized, and unsigned outputs leave stdout empty |
| Agent overrides `task_kind`, locale, or source inside its envelope | Trusted and untrusted policy fields share one JSON object | Permit only `target_text` and `release_token` from the agent; reject every host-owned field | Override attempts block before signature verification |
| Agent bypasses the wrapper through Telegram or an API credential | The process can reach the final channel directly before its actual response reaches the lifecycle gate | Deny Claude Telegram send and reply tools in `PreToolUse`; remove channel credentials and network routes from the agent; expose only a host-owned guarded sender after verified `Stop` | The observed Telegram reply path blocks without exposing the candidate, a read-only Telegram tool remains available, and the host end-to-end denial test proves direct delivery is impossible |
| Missing verifier key silently creates a second trust root | Every valid MCP receipt suddenly fails after restart | The signer may initialize the key; every delivery boundary, including the portable and installed-skill hooks, only loads an existing owner-only key and fails closed when absent | Missing-key tests cover both portable hooks, create no file, and emit no candidate |
| Repeated invalid `Stop` output reaches Claude's consecutive-block cap | Claude overrides the hook after repeated `decision: block` results and ends the turn with unverified text | Allow one correction cycle; when `stop_hook_active` is already true, accept only a newly verified exact answer or terminate processing with `continue: false` | Invalid `Stop` and `SubagentStop` hard-stop on the second attempt, while a corrected signed answer passes |
| A malformed `stop_hook_active` value bypasses Claude's protected correction-state handling | The hook treats only the exact boolean `true` as a repeated stop, so strings, numbers, objects, or a missing field fall back to the first-attempt block path | Require Anthropic's documented boolean type before reading any protected stop state and hard-stop malformed inputs without consuming a delivery grant | Malformed main and subagent stops terminate immediately, expose no candidate, and leave their exact one-time grants usable by a subsequent valid stop |
| Readable Morse output bypasses the final-response language gate | Dots and dashes are Unicode punctuation rather than letters, so a complete encoded word can pass without a signed delivery grant | Classify only runs of at least three separated valid Morse letter codes containing both dot and dash; normalize standard typographic variants while leaving isolated marks, ellipses, and short separators compatible | Literal, HTML-encoded, typographic, emoji-prefixed, main-agent, and subagent Morse text blocks; decorative one- and two-token controls remain allowed |
| Wrapped Morse or the compact SOS prosign bypasses the final-response gate | The separated-code matcher requires whitespace after its last token and does not recognize the conventional separator-free SOS signal | End separated Morse runs at a dot/dash boundary instead of requiring trailing whitespace; recognize only the exact bounded compact `...---...` prosign | Parenthesized, punctuation-suffixed, emoji-adjacent, HTML-encoded, typographic, and compact SOS forms block; short, incomplete, and overlong controls remain allowed |
| Roman numeral symbols spell readable words outside the Unicode letter category | Characters such as `Ⅽ`, `Ⅰ`, and `Ⅴ` are `Letter_Number`, so contiguous text like `ⅭⅠⅤⅠⅭ` reaches `Stop` without a grant | Compatibility-normalize contiguous Roman-symbol runs and classify multi-symbol text only when it cannot be parsed as a non-increasing additive/subtractive Roman number | Literal, lowercase, compound-symbol, HTML-encoded, main-agent, and subagent disguised words block; canonical and additive numeral controls remain allowed |
| An interrupted turn leaves a valid delivery grant for the next user turn | The same short or generic text can pass later without a new release call | Store a session hash with each hook record and remove only that session's unconsumed main-agent and subagent grants at `UserPromptSubmit`; block the prompt if a matching record cannot be invalidated | Cross-turn replay blocks, same-session child records disappear, and a parallel session's record remains usable |
| A subagent reaches `SubagentStop` without knowing it needs its own release | Main-session context is not reliably available before the child agent's first prompt | Inject the exact response and translation workflow at `SubagentStart`, including agent-bound fresh grants and fail-closed behavior when health is unavailable; retain `SubagentStop` as the enforcement boundary | Healthy and unavailable startup contexts use the exact event name, and unsigned subagent output still hard-stops |
| A failed MCP release call leaves an earlier grant usable | Claude retries after a disconnect but can still finish with pre-failure state | Match only failed release tools at `PostToolUseFailure`, delete the exact session-and-agent record before injecting retry guidance, and block if deletion fails; keep `Stop` and `SubagentStop` authoritative | Same-agent stale output blocks, another session remains usable, unrelated tool failures are ignored, and hook output contains no candidate or error text |
| A successful MCP call with a missing or rejected receipt leaves an earlier grant usable | Transport success is mistaken for release success, or concurrent `PostToolUse` hooks complete out of order | Clear the exact session-and-agent state before every release attempt and again on every logical rejection, verifier outage, or write failure; block if either cleanup fails | Missing receipt, rejected receipt, unavailable verifier, uncleared state, parallel-session isolation, and ordinary success regressions all pass |
| Resume, clear, or compaction reuses an unconsumed grant from an earlier Claude lifecycle | Mid-session hooks are replayed from the transcript while the same `session_id` can continue | Rotate an owner-only random epoch on every `SessionStart`, invalidate that session's records, bind only the epoch hash into the service-signed grant, and require the live epoch locally and at consumption | Copied pre-resume state, missing or unsafe markers, failed rotation, wrong epoch, old records, and cross-session isolation all block or remain isolated |
| Restoring both the old hook record and its matching epoch marker revives a pre-resume grant | The final hook and signed grant agree because both local files were rolled back together | Register each fresh epoch with the isolated service, atomically require the current service-side hash for grant issue and consumption, and reject every previously registered epoch during that service boot | Two-file restoration, stale authorization, and epoch re-registration block while a fresh epoch and parallel session pass |
| Restarting the isolated service strands every currently open Claude session | Old grants block correctly, but the new service has no epoch until another `SessionStart` | After verifying a fresh signed release receipt, allow `authorize_delivery` to enroll only a missing epoch; never enroll during consumption or replace a different current epoch | Old grants and forged receipts block across restart while the first fresh release restores exact one-time delivery without restarting Claude |
| A Claude API failure leaves an already issued grant usable by a later retry | `Stop` never runs when the turn ends through `StopFailure` | Silently remove every unconsumed main-agent and subagent grant for the exact session; rely on no ignored hook output or exit decision | Rate-limit failure clears both same-session grants, preserves a parallel session, leaks no error or candidate, and both stale deliveries block |
| Restoring local state after `StopFailure` revives an old service grant | Version 6.21 deletes only the hook record while the service keeps the same current epoch | Rotate the service-authoritative session epoch, remove the old local marker first, and write its replacement only after service confirmation | Restored record plus matching old marker blocks, parallel session passes, fresh release recovers, and unavailable service leaves no usable local epoch |
| An ended Claude session leaves its service epoch usable, or delayed cleanup retires a newer lifecycle | `SessionEnd` deletes only local files, or an unconditional service mutation races with a later `SessionStart` | Delete the exact session's local epoch and grants first, then compare-and-swap the service epoch to an undisclosed tombstone within the documented cleanup budget | Restored ended-session state blocks, stale retirement cannot replace a new epoch, a parallel session passes, and resume restores fresh delivery |
| Child process reads a signing secret from inherited environment | Agent can mint arbitrary receipts | Strip key variables from child environments and isolate the signer under a separate identity for hostile workloads | Child environment test cannot observe the secret; production sandbox cannot read the key file |
| Agent logs candidate text on stderr before the gate | Rejected output still reaches a user through combined logs | Capture both stdout and stderr; discard child diagnostics at the user boundary and emit only generic host errors | Rejected candidate appears in neither stdout nor diagnostics |
| Streaming reveals the draft before final verification | `text-delta` events reach the renderer while the final envelope later blocks | Buffer model text and `done` events; forward only non-text activity until exact receipt verification | Invalid envelopes produce no visible delta in desktop and Telegram tests |
| MCP and verifier share the same signing key file with the agent | A compromised agent mints arbitrary receipts | Move signing and verification into an isolated loopback service; give adapters only endpoint and service authentication | Child environment lacks both secrets; service-owned-key round-trip passes |
| Guard service dies and the host silently falls back | Outputs continue during service restart or token failure | Mark the service required in host policy and reject local-key fallback | Unavailable, unauthorized, and timeout probes never call a sender |
| Audit logging leaks customer prose or credentials | Rejected text appears in JSONL or diagnostic logs | Store bounded labels, canonical hashes, result codes, and version only | Tests search audit bytes for source, target, receipt, and service token |
| Audit log, lock storage, or their parent directory is redirected, raced, or replaced by a special file | The signer appends to an unrelated target, blocks on a FIFO, loses a decision in an unlinked file, writes through an exchanged directory, or reports healthy while real releases fail | Open every parent component without following links, hold one verified directory handle across lock and log access, open only regular single-link owner-controlled append files relative to it, create them exclusively as `0600`, recheck directory and file identities, and keep health inspection read-only | Symbolic and hard links, FIFO, unsafe parent permissions, external write access, and file or parent exchange races block without changing substituted targets; missing health paths remain absent and unsafe paths turn health red |
| Free-form chat is trusted to classify its own translation | Agent labels a translation as a response and skips the skill | Require structured host metadata and independently captured source; block contradictory or incomplete routes | Response/source downgrade and translation-without-source regressions block |
| BLUN Code changes from legacy `mcp.json` to its encrypted MCP store | Guard appears installed but the runtime exposes no release tools | Import the named legacy guard entry once through BLUN Code's own encrypted store and preserve existing servers | Bootstrap is idempotent and live MCP inventory includes both release tools |
| BLUN's legacy MCP configuration or its parent directory is redirected, writable, oversized, malformed, or exchanged during migration | Installer overwrites another file, loses a concurrent server change, or BLUN Code imports an attacker-selected endpoint and token path through substituted trust state | Retain a component-by-component validated parent-directory handle, read the bounded owner-controlled single-link regular file relative to it without following links, validate the complete guard entry, recheck identities before import or atomic replacement, and write owner-only backup and target files | Parent links, unsafe parent modes, missing read-only paths, exchange-and-restore races, file links, hard links, FIFO, excessive size, malformed fields, and identity races block before configuration or encrypted-store mutation; unrelated servers and safe `0644` files remain compatible |
| Claude's user configuration is redirected, writable, oversized, malformed, or exchanged during MCP repair | Installer overwrites another file, loses a concurrent Claude setting, or mutates runtime before discovering unsafe configuration | Preflight a bounded owner-controlled single-link `~/.claude.json` before runtime mutation, read without following links, recheck identity immediately before atomic replacement, and write owner-only backup and target files | Links, hard links, FIFO, unsafe modes, excessive size, malformed JSON, and identity races block without changing substituted targets; unrelated settings and safe `0644` files remain compatible |
| The updater refreshes the skill but not the host adapter | New receipt rules deploy while BLUN Code still uses an old envelope verifier | Version the portable adapter contract, test both repositories, and release host integration separately | Compatibility test covers the installed service version before delivery |
| Claude loses a child `stdio` MCP process | The tools disappear until the next session, even though the isolated guard is healthy | Prefer a stateless loopback Streamable HTTP MCP, run it independently with OS restart, and register it at user scope | Kill or corrupt one request; the service survives or restarts and a fresh initialize plus tools/list succeeds |
| A stale project-local Claude MCP shadows the repaired user server | The guard works in one repository and drops in another | Remove same-name local entries from `~/.claude.json` during installation and make `doctor` fail on remaining shadows | User config contains one HTTP entry and zero local same-name shadows |
| Project MCP shadow inspection follows or ignores unsafe state | A linked, oversized, malformed, broadly writable, or concurrently exchanged `.mcp.json` blocks inspection, leaks another file, or makes `doctor` report no shadow | Read every candidate with a bounded no-follow regular-file contract, validate its schema and stable identity, and fail the precedence check closed on any unsafe candidate | Safe checked-in configuration remains compatible; link, hard-link, FIFO, permissions, size, schema, and identity-race fixtures all fail without changing their targets |
| A browser reaches the local MCP through DNS rebinding | An external origin can invoke guard tools on localhost | Bind only to loopback, validate `Origin`, require bearer authentication, and keep the service token separate | External origin and missing token return 403/401 while an authenticated local client succeeds |
| The Claude access token rotates while a connection is cached | Reconnects keep sending the previous Authorization header | Use Claude's `headersHelper` so the owner-only token file is read for every connection and retried after 401/403 | Token-file rotation changes helper output without editing Claude configuration |
| Claude calls the release tool for one draft and returns edited prose | Tool usage is treated as proof without binding it to the final message | Independently verify the MCP receipt in `PostToolUse`, store only its exact target hash, and consume it once in `Stop` or `SubagentStop` | Exact target passes once; edited target and replay both block |
| A subagent bypasses the main agent's language rule | Only the top-level `Stop` event is guarded | Install the same receipt-consuming gate for `SubagentStop`, keyed by Claude session and subagent identity | Main and subagent fixtures each require their own verified record |
| MCP fails but Claude finishes anyway | A skill instruction is remembered while the release service is unavailable | Inject fail-closed session context and require a fresh isolated verification record at stop time | Unavailable service creates no release record and the stop hook blocks |
| Claude plugin update is downloaded but an old session keeps old hooks | Plugin cache changes do not replace live hook and MCP paths mid-session | Keep the persistent service independently updated; require `/reload-plugins` or a new session before claiming the plugin update active | Version check distinguishes downloaded, loaded, and service versions |
| Claude reaches its stop-hook continuation cap | Repeated invalid responses exceed Claude's built-in loop protection | Treat hooks as workflow enforcement, deny direct delivery credentials, and use a buffering host for non-bypassable output | Host-level test proves no model text reaches the channel without verification |
| Rendered natural language contains no literal Unicode letter in the Stop payload | Decimal HTML character references, including references without their optional semicolon or with arbitrarily many leading zeroes, render as readable prose after the raw output check, while a blanket entity or Unicode-mark rule also blocks harmless emoji-only output | Decode complete numeric character references for classification with or without a semicolon; ignore leading zeroes while bounding significant digits and rejecting partial overlong matches; treat decoded letters and linguistic combining marks as natural language, but exclude emoji variation selectors and the enclosing keycap mark | Plain, zero-padded and semicolonless decimal German prose plus literal and encoded combining accents require a grant in Stop and SubagentStop; literal, decimal and hexadecimal emoji-only sequences, including zero-padded presentation selectors and keycaps, remain compatible |
| Named HTML references create raw-letter false positives | Spacing, punctuation, and symbol-only output such as `&nbsp;`, `&hellip;`, or `&copy;` is classified as language because the entity names contain ASCII letters | Strip only an exact case-sensitive allowlist of standardized named references whose rendered values contain no letters or linguistic marks; leave language-bearing and unknown names visible to the fail-closed classifier | Named spacing, punctuation, and symbols pass Stop without a grant; `&Auml;`, `&aring;`, unknown names, and mixed natural language still require verification |
| Invisible HTML formatting references create raw-letter false positives | Output containing only standardized controls or spacing such as `&Tab;`, `&ZeroWidthSpace;`, or `&InvisibleTimes;` is classified as language because the entity names contain ASCII letters even though their rendered values contain neither letters nor linguistic marks | Extend the semicolon-required exact allowlist only with verified WHATWG formatting references whose complete decoded values contain no Unicode letter or mark; keep semicolonless, unknown, and language-bearing names fail-closed | Every selected formatting reference passes alone and as a sequence; the same names without semicolons, near-miss names, linguistic entities, and mixed prose still require verification |
| Standardized named HTML mathematics and symbol references create raw-letter false positives | Output containing only references such as `&sum;`, `&rarr;`, `&boxVH;`, or `&clubsuit;` is classified as language because the entity names contain ASCII letters | Generate an exact semicolon-required allowlist from the WHATWG entity table and include only names whose complete rendered values contain no Unicode letter or linguistic combining mark; keep unknown, language-bearing, combining-mark, semicolonless non-legacy names, and mixed prose fail-closed | The bundled table and production classifier match all 2,125 standardized semicolon names, every legacy and non-legacy semicolonless form, and every safe reference followed by text |
| Legacy named HTML references omit their semicolon | Browsers render a fixed legacy subset such as `&nbsp`, `&copy`, and `&frac12` as non-language characters, but the raw entity names trigger the Stop language classifier | Accept only WHATWG legacy names whose decoded values contain neither letters nor linguistic marks; consume the longest safe prefix and retain every suffix for classification | Semicolonless spacing, punctuation, currency, and numeric symbols pass without a grant; language-bearing, non-legacy, unknown, and natural-language suffixes still require verification |
| Numeric HTML C1 references render differently from their Unicode code points | The raw control values `&#x8A;`, `&#x8C;`, `&#x9A;`, or similar contain no Unicode letter, but the HTML parser applies its Windows-1252 replacement table and renders readable Latin letters after Stop inspection | Apply the standard HTML C1 numeric-reference replacements before classifying decoded output; retain the existing Unicode letter/mark rule on the rendered code point | Decimal, hexadecimal, zero-padded, and semicolonless C1 references that render as letters block in Stop and SubagentStop; C1 references rendering only currency, punctuation, or symbols remain allowed |
| Enclosed Latin letters are Unicode symbols instead of letters | Parenthesized, circled, squared, negative-circled, or negative-squared glyphs render readable words such as `Ⓗⓔⓛⓛⓞ`, but a category-only letter check lets them pass Stop without a grant | Classify the exact Unicode ranges assigned to enclosed Latin letters; allow one character with the Unicode `Emoji` property, but classify two or more enclosed emoji letters as language | Readable enclosed-letter text and short words made from letter-button emoji block in Stop and SubagentStop; individual established enclosed emoji buttons remain allowed without a natural-language false positive |
| Regional indicator symbols spell readable output outside Unicode's letter category | Separated flag-letter symbols such as `🇭 🇪 🇱 🇱 🇴`, including numeric HTML references, render as letters while a category-only check treats them as emoji | Decode numeric references into the same non-recursive visible stream; pair only adjacent regional indicators as flags and classify two or more remaining indicators as language | Literal, zero-width-separated, decimal, hexadecimal, and mixed letter sequences block in Stop and SubagentStop; single indicators and adjacent flag pairs remain allowed |
| Braille text is encoded as Unicode symbols instead of letters | Literal or numerically HTML-encoded Braille cells carry readable multilingual text while a category-only check lets the complete output pass without a grant | Count nonblank Braille cells in the decoded visible output stream and classify two or more cells as language; leave the blank pattern and one isolated marker compatible | Literal, spaced, decimal, hexadecimal, and mixed Braille text blocks in Stop and SubagentStop; blank patterns and individual cells remain allowed |
| SignWriting text is encoded as Unicode symbols instead of letters | Handshape, movement, and location sequences carry written sign language while a category-only check lets literal or numerically encoded output pass without a grant | Count characters only in the assigned SignWriting symbol range and classify two or more as language; keep one isolated symbol and the separate punctuation range compatible | Literal, spaced, decimal, hexadecimal, and mixed SignWriting sequences block in Stop and SubagentStop; individual symbols and pure SignWriting punctuation remain allowed |
| A failed temporary Claude grant or session epoch is exchanged after its cleanup identity check | Cleanup deletes the replacement path while the originally created file survives elsewhere | Keep the creation descriptor open, move the pathname to an unpredictable cleanup quarantine, and compare the expected creation identity, open descriptor, and quarantined path before unlinking | Deterministic late-swap probes preserve both the originally created file and its replacement while grant or epoch publication blocks fail-closed |
| Same-user agent tampers with a grant, session-epoch state file, or its parent directory | Plugin hooks and the model share filesystem authority | Store no prose; require bounded, owner-only single-link regular files; bind the hard-link count into the read identity and revalidate that identity on the same descriptor after every read; retain a component-by-component validated parent-directory handle while reading, writing, agent-specific and session-wide invalidating, and consuming grants and while reading, renewing, and consuming epochs; validate every agent-specific and enumerated grant and bind each invalidation to its inspected identity; validate existing grant and epoch renewal targets, bind epoch removal to its inspected identity, and publish replacements only over the inspected grant identity or continued safe absence; apply owner-only permissions and validate metadata on the still-open temporary descriptor before publication, bind temporary source, published result, and cleanup to the created file identity, and never change permissions through the published path; open files without following links where supported; atomically publish grants and epochs through unpredictable exclusive temporary files relative to the held directory; use managed hooks and separate-user or remote enforcement for hostile workloads | Broad permissions, hard links, parent links, missing, writable or exchanged grant and epoch directories, in-place mutation during descriptor reads, grant-write, agent-specific and session-wide invalidation, epoch-write, temporary-source exchange, temporary hard-link creation, cleanup exchange, post-publication file exchange, post-publication permission races, grant- or epoch-publication-target exchange, or removal-time exchange-and-restore, symlinks, oversized records and epochs, predictable temporary links, and replaced file identities fail closed without creating state in an unverified directory, changing permissions on substituted state, overwriting substituted state, or deleting substituted state; threat-model documentation never labels same-user hooks non-bypassable |
| A grant or session epoch is exchanged after its final identity check but before unlink | The pathname-based delete can remove an uninspected replacement even though every earlier check passed | Move the candidate to an unpredictable quarantine path inside the retained protected directory, keep its descriptor open, and require the descriptor and quarantine pathname to retain the inspected identity before unlink | Deterministic late-swap chaos probes preserve both the inspected original and substituted grant or epoch, leave the substitute quarantined, and block before service registration or grant consumption |
| A grant or session epoch grows after its size precheck | `readFileSync` can consume attacker-appended bytes until EOF before the final metadata comparison notices the change | Read from the already-open descriptor into a fixed limit-plus-one buffer, reject overflow before parsing, and decode only the complete bounded payload as strict UTF-8 | Grant and epoch growth after the first descriptor read blocks at their exact byte limits; every individual read request and total retained buffer remain bounded |
| Session startup recursively creates an unverified Claude hook state path | A missing state directory below a linked, broadly writable, or concurrently exchanged parent is created outside the intended runtime before later epoch checks can reject it | Create each missing component relative to a no-follow opened and owner-controlled parent-directory handle, retain the final handle through a path-identity check, and only then register or publish the new session epoch | Safe nested first-use creation succeeds with owner-only directories; linked and writable parents receive no new directory; an exchanged parent receives no state and startup blocks before service registration or epoch publication |
| A forged or copied Claude hook record authorizes output | `Stop` trusts a local target hash after `PostToolUse` | Exchange the verified receipt for a service-signed grant bound to target, session, agent, version, service boot, and expiry; delete locally before server-side one-time consumption | Exact grant passes once; copied, forged, edited, cross-session, cross-agent, restarted-service, and replayed grants block |
| Hardened Claude plugin bytes retain the previous explicit version | Claude Code keys marketplace updates by manifest version, so existing installations skip the new hooks as already current while the skill may advertise another stale version | Bump the explicit plugin version for the hardened package and bind `VERSION`, the manifest, active skill contract, and README current-version reference in a regression test; require every later plugin release to advance the explicit version with its changed bytes | Version 6.42.0 is identical across active contracts; substituting a stale skill, manifest, or README version fails repository tests before publication |
| Runtime auto-update leaves Claude on an older cached plugin | Repository, signer, and MCP advance while third-party marketplace auto-update is off or a cache update fails | Update only an already-installed user plugin through Claude's official CLI, verify the exact version via `plugin list --json`, record degraded state on mismatch, and never claim live activation before reload | Fake isolated Claude CLI proves success, failure, missing-plugin no-op, exact-version verification, and reload notice without touching user configuration |
| Automatic plugin repair uses a stale marketplace catalog or races ahead of the tested runtime | `plugin update` installs the latest version known to Claude, but the catalog was never refreshed or now advertises a different candidate | Refresh only the trusted marketplace, require its public available version to equal the tested repository version, then update and verify the installed version | Refresh failure and catalog drift block before plugin mutation; an exact catalog reaches the exact cache version; an already healthy cache remains untouched |
| A repository candidate passes project tests but Claude rejects its plugin schema | An unknown manifest field, invalid hook schema, or malformed skill metadata reaches the cache because only repository-owned parsers ran | Run Claude's documented `plugin validate <root> --strict` against the exact tested plugin root before marketplace refresh or cache mutation | Strict-validation failure leaves the installed version unchanged and makes no marketplace or plugin update call; a valid candidate preserves the existing ordered checks |
| Claude plugin discovery fails only after the runtime cutover | The repository, signer, and MCP advance while validation, marketplace refresh, or catalog equality then fails against an old Claude cache | Preflight the clean tested candidate with Claude's strict validator and exact trusted-catalog check before fetch, merge, restart, or installed plugin-cache mutation; consume that successful expected-version preflight at application | Validator, refresh, catalog, and process-loss failures preserve the active commit, runtime files, services, and installed plugin cache while recording an immediate degraded retry |
| A health monitor creates a restart storm or races an update | Guard and MCP repeatedly restart while files are changing, yet the monitor reports recovery | Probe the full signer and MCP path, share an atomic stale-safe maintenance lock, repair dependencies in order, permit one repair per run, persist bounded exponential backoff, and require a final end-to-end probe | Healthy no-op, MCP-only, signer-first, multi-run backoff, reset, overlap, and Linux/macOS/Windows scheduler tests pass without touching live services |
| Two localization service supervisors overlap, crash while leased, stop while ready, or persist a private adapter exception | Providers or CMS receive concurrent calls, a dead process blocks progress forever or still appears healthy, a stale process overwrites recovered state, or customer prose reaches operational status | Claim one transactional expiring supervisor lease, bind completion to worker and random token, retain the pipeline's narrower operation leases, apply a stale-heartbeat threshold, use bounded backoff and stop polling, and persist only validated tick metadata plus fixed exception codes | Two SQLite connections prove live exclusion, expired-lease recovery, overdue-heartbeat degradation, stale completion rejection, graceful stop, capped backoff, state tamper blocking, and exception redaction |
| A localization host assembles independently loaded adapter classes, reuses one SQLite connection, or discovers a missing capability after claiming work | The valid service bridge fails health by class identity, unrelated schemas or transactions collide, or a locale remains leased without a configured publisher or verifier | Construct all stores from one canonical service module, let health accept the exact structural bridge contract, require five distinct idle host connections, validate the exact frozen dependency mapping before schema creation, and keep connection ownership with the host | One composed runtime takes a signed Finnish event through translation, evidence, approval, CMS publication, supervision, and health; invalid capabilities and duplicate connections produce zero schema writes |
| A service supervisor lease expires before its translation, evidence, or CMS child lease | A second process claims the outer singleton while the first tick still legitimately owns an inner operation, causing overlapping provider or publication calls | Require the supervisor lease to be strictly longer than every effective child lease before schema writes, then renew the exact outer token immediately before each child external operation and reject every expired finish | Each child lease fails against an equal outer lease with zero tables created; translation, evidence, and delivery invoke the guard; renewal excludes a second instance past the original expiry; an expired worker cannot finish without a takeover |
| A production host supplies a cache that invents or returns unsigned translations | An offline model appears to succeed with a target that has no exact signed approval, or a corrupt cache falls through to an external provider | Reject every host-supplied result cache before schema creation; bind the runtime's fallback only to its own signed local translation memory and validated approval authority; treat expiry as a miss and tampering as a blocking attempt | A fresh operational runtime restores an exact approved result without asset or provider calls; stale policy misses; tampered memory blocks before those calls; injected caches create no tables |
| A language or domain reviewer returns PASS despite low confidence | A grammatical but weak or poorly evidenced locale reaches approval because confidence was omitted, averaged, or treated as a score; a nominally second model may secretly reuse the primary adapter | Require exact high/low confidence from both ordered reviews; let high change nothing; bind either low decision into `independent_review_required`; accept exactly one separately verified qualified-human receipt or differently identified provider adapter; retain human-only review for legal content | Missing and unknown confidence block; low confidence creates a valid but unreleasable result until one independent route verifies; same-provider, ambiguous, malformed, stale, or rejected evidence blocks; the signed approval binds the chosen method, model identity, and receipt hash; legal and major-defect gates remain stricter |
| One generic or substituted language profile governs every EU locale | Reviews overlook locale-specific morphology, information structure, script, calques, and CTA conventions, while an old result survives a profile change | Maintain one canonical versioned and hashed profile per exact BCP-47 locale; bind it through job identity, all model phases, benchmark input, worker result, evidence request, and signed approval | Registry tests cover all 24 locales and the full adversarial matrix; Finnish and Maltese fixtures assert their required focus; missing, stale, and tampered profile bindings block before provider use or release |
| Health-monitor policy or backoff state is redirected, oversized, broadly readable, malformed, or exchanged during inspection | A linked `enabled: false` silently disables monitoring, corrupt state resets repair backoff, or a special file blocks the minutely runner | Read both health files as bounded owner-only regular files, never follow links, compare identity before and after reading, validate persisted field types, and block before probing, repair, update candidate execution, or state replacement | Link, permission, size, schema, and identity-race probes fail closed; no service repair or candidate command runs and linked targets remain unchanged |
| Health-monitor removal deletes redirected or concurrently replaced state | Reset follows a link, removes an unrelated replacement, or changes the scheduler before discovering unsafe policy | Preflight both policy and state with the protected reader, bind replacement and deletion to their exact identities, and block before scheduler mutation when either file is unsafe | Linked policy and state leave the scheduler and targets untouched; a state exchanged after preflight survives and the reset reports a blocked result |
| Health-monitor activation overwrites a concurrently replaced policy | The scheduler starts against unreviewed settings or activation erases an operator change made after the initial health probe | Read and bind the exact policy after probing, recheck it before scheduler mutation and immediately before atomic replacement, then remove a newly installed schedule if the final check detects an exchange | Pre-scheduler and post-scheduler exchange tests preserve replacement bytes; only the latter invokes the matching scheduler rollback |
| Claude plugin auto-enrollment overwrites a newer health policy | Claude's status command runs while an operator disables or reconfigures monitoring, then the stale monitor snapshot replaces that policy | Bind automatic plugin enrollment to the exact policy identity read at the start of the minutely run and block the run before repair or state publication when it changed | An exchange during plugin inspection preserves the replacement, performs no plugin update, writes no health state, and returns the fail-closed blocked result |
| A minutely monitor run overwrites newer backoff state | A probe or repair races an operator or another protected state transition, then publishes counters calculated from stale bytes or restarts a service despite the newer backoff | Bind every health-state write to the identity read at run start and recheck that identity immediately before beginning any repair | Exchanges before repair prevent all restart and plugin-update calls; exchanges during repair remain intact and the stale run publishes no state |
| A minutely monitor run repairs against a replaced health policy | An operator disables or reconfigures monitoring during a probe or repair, but the stale run still restarts a dependent runtime, updates Claude, or publishes the old decision | Bind every repair and health-state publication to the policy identity read at run start; refresh it only after protected automatic enrollment | Exchanges during probing block before every repair; exchanges during repair preserve the new policy, prevent dependent repair, and publish no stale state |
| A long valid maintenance operation outlives the lock timeout | After 30 minutes the monitor removes a live updater lock and restarts services while candidate tests or cutover are still running | Treat age only as eligibility for inspection; preserve every validated lock whose PID is alive, recover only a dead or untrusted old owner, and compare file identity again immediately before release or stale removal | An hours-old live-owner lock remains; a confirmed dead-owner lock recovers; concurrent replacement survives both stale recovery and normal release |
| The shared maintenance lock or one of its parent directories is redirected, linked, broadly writable, or exchanged during inspection | Another account can redirect lock creation, an unrelated hard-linked file controls updater availability, or a process trusts a different directory or inode than the one it created or read | Open every parent below the user home without following links, retain the final directory handle, accept only a bounded owner-only single-link regular file, and bind creation, reads, stale recovery, and release to those stable identities | Parent-link, parent-permission, parent-exchange, file-link, file-permission, read-race, creation-race, recovery-race, and release-race fixtures preserve foreign data and block fail-closed |
| A crashed maintenance process leaves a PID that the OS later reuses | The lock sees a live numeric PID owned by an unrelated process and blocks updates or repair indefinitely | Bind every new lock to an OS process-start identity; recover only on a definite generation mismatch, preserve ambiguous and legacy live owners, and never signal a process while inspecting it | Matching, reused, unavailable, legacy, Linux/POSIX and Windows generation probes prove recovery without weakening fail-safe locking |
| Automatic update runs over local or concurrent checkout changes | A fast-forward succeeds beside non-overlapping edits, including work created while the network fetch, cutover, or post-update tests are in progress, leaving an untested mixture of guard versions | Require a valid clean `HEAD`, including every untracked path, before candidate execution, after fetch, immediately after fast-forward, and after post-update tests before runtime activation; use `reset --keep` only when the candidate is still `HEAD`, never reset or execute an independently advanced commit | Initial, preflight, fetch-time, cutover-time, and passing or failing post-test dirty/commit races all block runtime activation; uncommitted bytes survive safe rollback and independent commits are never rewritten |
| Update or rollback overwrites a newer updater-state decision | Candidate testing or runtime verification races a concurrent recovery process, then stale `ok`, `degraded`, or `rolled_back` state replaces the newer decision | Retain the exact updater-state identity read at operation start, recheck it before activation or rollback policy mutation, and bind every atomic state publication to it | Candidate-time exchange blocks before fetch; rollback-time exchange restores the forward revision; replacement state survives and no stale status is published |
| Forward update overwrites newer health policy or backoff state | Scheduler installation or plugin maintenance races an operator opt-out or a newer monitor decision, then stale updater snapshots replace the protected files | Retain both health-file identities from update start, recheck them before scheduler activation and every atomic health write, refresh identities only after the updater's own protected writes, and skip plugin maintenance after any conflict | Policy- and state-exchange fixtures preserve replacement bytes, publish degraded updater status, make no plugin update, and remove the just-activated schedule only for a concurrent opt-out |
| Services are green while Claude's mandatory hooks are stale or disabled | Signer and MCP probes pass, but `Stop` and `SubagentStop` come from an unhealthy plugin cache | Enroll only an observed installed plugin, verify enabled state, load errors, and exact version through Claude's public CLI, repair under the shared lock and backoff, never auto-install, and never claim an active session reloaded | Stale installed cache updates and rechecks; missing enrolled plugin blocks without any install or update command |
| Emergency rollback mixes old signer code with new Claude hooks, races local work, or is immediately undone | Recovery appears successful, concurrent bytes enter an untested checkout during preflight, cutover, post-tests, or runtime verification; an operator commit is rewritten or executed; receipts fail; or the scheduler reinstalls the rejected revision | Accept only the updater-recorded ancestor; require the same completely clean exact `HEAD` before and after preflight, immediately after `reset --keep`, after post-rollback tests, and after runtime and Claude-cache probes before scheduler or state mutation; safely restore the forward revision and runtimes for uncommitted races, never reset or execute an independent commit, preserve signed-commit policy, require an already matching Claude cache, and pause automatic updates only on exact success | Success, initial dirty-tree, preflight dirty/commit, cutover dirty/commit, post-test dirty/commit, runtime-verification dirty/commit, stale-state, plugin-mismatch, target-test, runtime-failure, and updater-pause regressions |
| Rollback pause overwrites or moves concurrently replaced updater policy | Runtime verification passes, then a stale path-based move replaces an operator policy or pauses different bytes than were tested | Capture active and paused policies before candidate execution, recheck both exact identities after runtime verification, atomically write the inspected active policy to paused storage, and remove only the unchanged source | Active- and paused-policy exchange races preserve replacement bytes, do not remove the scheduler, and restore the forward revision and runtimes |
| Health reports green while MCP tools cannot execute or the signer cannot issue valid receipts | Heartbeat, initialize, and tools/list succeed, but every release call fails | Make authenticated health perform an audit-free release/signature/tamper self-test and require a real multilingual MCP `tools/call` before declaring the stack healthy | Broken signer and dispatcher controls block; a temporary TCP signer plus HTTP MCP completes the Swedish deep probe end to end |
| A valid Claude delivery grant is detached from its translation context | `Stop` verifies target, session, and agent but does not recheck source binding, language, purpose, or content policy | Sign the complete context into the one-time grant and require exact hashes and metadata again at consumption without storing source prose | Exact translation passes; changed source hash, locale, purpose, content type, review flag, or channel blocks and burns the grant |
| Malformed Claude identity fields collide with a valid session or agent | JavaScript coerces non-string `session_id` and `agent_id` values such as `{}` to `"[object Object]"`, so two distinct hook inputs can address the same local grant and service identity | Accept only the documented non-empty string identity fields, default `agent_id` to `main` only when it is absent, reject NUL-delimited ambiguity, and reuse those exact values for state paths and signed service requests | Object, array, numeric, empty, and NUL-bearing identities block before touching protected state; a legitimate string identity remains isolated and can still consume its own grant |
| A malformed `SubagentStop` omits `agent_id` and consumes the main agent's grant | The shared identity helper treats every absent agent field as `main`, although Anthropic documents `agent_id` as part of every `SubagentStop` input | Give `SubagentStop` a dedicated trusted hook route, require its route and event name to match plus an explicit valid `agent_id`, and retain the absent-field fallback only for the main-thread `Stop` route | A missing child identity and a route/event mismatch both block before protected state is read; the exact main-agent stop can still consume its untouched one-time grant |
| A forged main-thread `Stop` supplies a child's `agent_id` and consumes the subagent's grant | The shared identity helper accepts an optional agent field on every event even though Anthropic adds `agent_id` specifically to `SubagentStop`, so the main route can address a child namespace | Make route-specific identity policy symmetric: forbid `agent_id` on `Stop`, require it on `SubagentStop`, and validate the policy before any protected state access | A main stop carrying a child ID blocks without consuming state, while the exact child stop still consumes its own one-time grant |
| Arbitrary easy or homogeneous benchmark cases support a superiority claim | A caller repeats headlines, omits difficult domains or swaps the source set while retaining favorable reviewer results | Bind policy, blind assignment, review request, case result, and report to one canonical versioned source-suite digest; require every suite case exactly once per locale and keep Maltese and Finnish mandatory | Tests cover all content types, distinct domains, long-form, HTML/JSON, offer semantics, every eligible EU locale profile, suite tampering, missing cases, and duplicate cases; the output-free suite itself makes no linguistic-quality claim |
| A valid benchmark result is relabelled as evidence for another candidate system | A report survives a provider, model, software, glossary, localization policy, locale profile, or suite-job change even though that exact candidate was never evaluated | Bind the exact candidate configuration and locale profile through benchmark policy, keyed blinding commitment, case result, canonical job reconstruction, and final report while keeping identities out of reviewer payloads | Every candidate dimension blocks before review when mismatched; aggregation rejects changed bindings, profile hashes, and job IDs; report fixtures retain the exact evaluated configuration without weakening blind review |
| A fabricated or altered benchmark result supports a public superiority claim | Hash-shaped fields and internally consistent winner counts can be invented without running either blind review, or a signed report can be detached from its original case set | Require a host-owned isolated attestation authority, sign and immediately verify each canonical text-free case result, verify every case before aggregation, bind the report to the exact signed-case digest, and attest the complete report | Missing, changed, foreign-key, rejected, and wrong-authority case attestations block; changed reports and substituted case sets fail public verification; reviewer requests remain origin-free |
| An arbitrary output is labelled as an official or lawful benchmark baseline | Baseline identity and target hashes are internally consistent, but nothing binds the text to an allowed acquisition route or retained evidence | Accept only `official_api` or `lawful_fixture` provenance, bind its stable evidence ID and digest into an immediately verified baseline attestation, and carry baseline-evidence hashes through case results and reports without exposing provenance to reviewers | Unsigned, scraped, undocumented, altered, wrong-key, and substituted baseline evidence blocks before the first review; both allowed routes pass with exact bindings and origin-free reviewer payloads |
| A model-generated or replayed target is presented as a native reference case | A formatted receipt accepts another source, locale, glossary, policy, profile, revision, or reviewer, or the reference prose reveals an origin to the A/B panel | Require a separately identified qualified-native human receipt verifier, bind its exact request to case, complete source, locale, content type, glossary and localization policy, locale profile, reference revision, target, and credential, attest and reverify it before review, and expose only reference hashes outside the artifact | Missing, foreign-key, changed, replayed, rejected, same-party, and mutating-verifier evidence blocks before review; reference text and identity remain absent from both ordered reviewer requests, case result, and report |
| Successful early benchmark lanes are published as EU-wide superiority | Maltese and Finnish pass while untested or failed target locales disappear behind `superiority_claim_allowed: true` | Separate configured-lane status from claim authorization, derive the exact eligible EU target scope from the bound suite source languages, attest missing and unexpected locales, and require every eligible locale report to pass | Passing Maltese and Finnish remain an explicitly blocked partial claim; only all 23 eligible targets for the English-source suite can produce a positive claim, while the exempt `en-IE` source-language locale remains visible |
| Cross-axis disagreements manufacture a statistically significant benchmark win | Cases where the candidate wins only nativeness or only fidelity become jointly inconclusive, so the smaller remaining set appears unanimous even though one required axis is weak | Report target-only nativeness and source-aware fidelity separately with predeclared sample, decisiveness, win-rate, and one-sided sign-test thresholds; require both axes to pass for every locale in addition to the joint metric | Six joint wins plus two nativeness-only wins retain a superficially significant joint result but fail the fidelity axis at `p = 0.14453125`; unanimous fixtures pass both axes |
| Commercial omissions or additions cannot be represented truthfully | Requiring both source and target spans makes a reviewer invent a counterpart for an omitted cancellation term or a target-only price, while empty defect items provide no actionable location | Version the public profile, add exact `matched`, `source_only`, and `target_only` relations with a nullable absent-side span, and require concrete items for dimension-level changed or uncertain findings | Source-only omissions and target-only additions reach their intended fail-closed outcomes; unknown relations, impossible span combinations, one-sided equivalence, and empty specific findings are rejected as malformed |
| A complete benchmark exists only as an in-memory loop | A crash repeats paid reviews, missing cases disappear from a report, changed policy reuses old evidence, or provider prose leaks through operational errors | Derive every suite-case/locale item from the bound policy, persist deterministic work IDs with expiring token-bound leases and bounded retries, store only attested text-free results, and require exact completion before summarization | A 23-locale campaign contains exactly 345 items; replay is idempotent; crash takeover, stale-token rejection, bounded failures, policy/state tampering, one-case ticks, result binding, and incomplete-report blocking all pass |
| Strong results outside pricing hide weak commercial localization | A provider wins headlines and UI while losing price, tax, renewal, or cancellation fidelity, yet the all-content locale aggregate still appears statistically significant | Version the suite and report, require eight diverse commercial cases per locale, and repeat joint plus independent nativeness/fidelity thresholds inside a mandatory commercial content-type lane | Thirteen joint all-content wins and six decisive commercial wins remain significant, but 6:2 commercial fidelity fails its lane axis and blocks the locale and overall claim |
| Strong results in one website content type hide weak results in another | A provider wins pricing and UI but has too few independently measured headlines, CTAs, marketing pages, documentation, SEO, or legal texts, while the all-content aggregate still authorizes a broad public claim | Version the source suite, report, and signed claim scope; require eight cases from eight domains for each of all eight content types; apply the joint and both independent quality-axis thresholds per type and locale | The 64-case corpus has exact 8-by-8 content/domain coverage; a weak commercial or headline fidelity lane blocks despite a strong aggregate; a deliberately partial content policy remains reportable but cannot authorize a public superiority claim |
| Baseline acquisition leaks credentials, silently changes language scope, or invents an unsupported comparison | A redirect receives the API key, a stale hard-coded language list misroutes a locale, multiple texts lose document context, or an unavailable Maltese baseline is replaced with generated prose | Allow only DeepL's exact official Free/Pro HTTPS origins with redirects disabled; query and briefly cache the stable `/v3/languages` capability response; send one complete source document per `/v2/translate` request; bind the exact request and response digests into provenance; and accept unsupported locales only through a separately attested, rights-bound fixed fixture | Transport fixtures prove exact origins, headers, one-text requests, dynamic Finnish support, fail-closed Maltese handling, bounded retry classification, strict response parsing, provenance tamper detection, and lawful-fixture binding without storing credentials or comparison prose in evidence |
| A benchmark worker crash repeats a paid baseline call or silently replaces the comparison text | An attested API or licensed-fixture baseline exists only in memory until both reviews finish, so restart obtains a newer output; a corrupt cache is treated as a miss; or concurrent workers overwrite one another | Persist the complete attested acquisition under a deterministic route, policy, and job identity before review; reverify the artifact, provenance evidence, hashes, schema, and authority on every read; block corrupt or conflicting state before any provider call; and run a lease guard immediately before each external request | Restart reuses the exact acquisition without another request; changed policy, baseline version, route, source, or locale cannot reuse it; tampering and same-key conflicts block fail-closed; concurrent identical writes converge; and request guards run before credentials or transport |
| A separately loaded benchmark adapter passes every unit test but cannot join the durable campaign | Each Python module owns a distinct `BenchmarkPolicy`, `BenchmarkSignature`, `BenchmarkCaseInputs`, or `BaselineAcquisition` class object, so exact `isinstance` checks reject structurally identical trusted values before the first real case | Normalize only frozen dataclass-shaped values with the exact public field set into the receiving module's canonical class, then run every existing policy, signature, job, evidence, and attestation check unchanged | A campaign policy and authority from one module copy drive a DeepL adapter and acquisition store from another; foreign input envelopes complete one case; missing, extra, malformed, and lookalike objects still block before review or persistence |
| A crash or retry silently replaces a qualified-native benchmark reference | The reference target and receipt exist only inside the host resolver, so a restarted case calls the external vault again, receives a changed target, or treats corrupt persisted state as a cache miss | Persist the first fully verified native-reference artifact under an exact route, policy, and suite-job identity before blind review; reverify its artifact attestation and qualification receipt on every read; block corrupt or conflicting state; and run the campaign lease guard immediately before an external lookup | Restart reuses the exact reference without another lookup; changed route, policy, source, locale, or reference revision cannot reuse it; tampering blocks before lookup; identical concurrent saves converge while conflicting valid artifacts never replace the first; and the durable reference completes a campaign case without leaking prose into campaign status |
| A crash or retry silently replaces the candidate being benchmarked | A validated candidate exists only in memory until both blind reviews finish, so a restarted case calls the attached model again and may compare a different output under the same campaign identity | Persist the first fully validated and host-attested worker result under an exact non-secret route, policy, and suite-job identity before blind review; reverify the store digest, candidate binding, and attestation on every read; guard every model and attestation operation; and reject conflicting same-identity outputs without replacement | Restart reuses the exact candidate without another model call; route, policy, source, locale, and model changes cannot reuse it; digest and resigned-content tampering block before provider access; identical concurrent saves converge while a different valid candidate conflicts; and failures enter the campaign's bounded content-free retry path |
| A host composes individually valid durable benchmark artifacts into the wrong case | Candidate, baseline, and native reference each verify alone, but an unrestricted input callback can mix routes, policies, jobs, or lease generations and repeat only one paid dependency after a crash | Use one preflighted benchmark execution root with five distinct idle stores, canonical policy and routes, and the same token-bound lease guard threaded through every external resolver and review; construct the final input envelope only from those exact durable lookups | Invalid configuration creates no schema; one campaign case crosses all stores; restart makes no second model, baseline, reference, or completed review call; mismatches and tampering block before blind review; campaign status remains free of source and target prose |
| Background benchmark execution delays a real localization or outlives its host lease | A combined service tick starts a paid benchmark case while a CMS delivery, release, or locale translation is actionable, or the inner campaign lease is not bounded by the supervisor lease | Preflight the complete benchmark execution configuration and all ten distinct stores, require the outer lease to strictly exceed the campaign lease, run the normal publication pipeline first, and advance at most one benchmark case only after that pipeline returns idle while threading the same token-bound guard through the case | Due delivery, release, and translation work never calls the benchmark executor; an idle tick advances one case; invalid or partial execution configuration writes no schema; lease loss blocks before external benchmark work; supervisor output remains content-free |
| A benchmark crash repeats or replaces an anonymous quality review | The first review succeeds externally but the process stops before the case result is committed, so a retry pays again, receives a different verdict, or reuses a response for changed blind variants | Persist each strictly validated review response in a dedicated store under its deterministic review ID; bind the canonical request and response hashes, benchmark policy, reviewer identity, and host attestation; verify all bindings before reuse and reject corruption or conflicts without another reviewer call | Restart reuses both exact ordered responses; changed requests, policy, reviewer, or route cannot reuse them; altered or re-signed evidence blocks before review; identical concurrent saves converge while conflicting responses remain immutable; campaign status stays content-free |
| Stored benchmark reviews disappear while completed cases still look valid | The campaign result attestation survives, but its referenced review rows were deleted, altered, rebound, or can no longer be verified, so ordinary campaign health misses a broken audit trail | Add a read-only review-store health check scoped to the exact active policy and route, reverify each immutable artifact, and require both pass request/response hash pairs referenced by every succeeded case | The check performs no writes or reviewer calls and returns only fixed codes and counts; missing, mismatched, tampered, or unverifiable current evidence blocks, while historical policies cannot satisfy current requirements |
| A completed campaign gets a different final report after a restart or health check | The final report is reconstructed and signed on every request, so signer changes, concurrency, or later state corruption can replace the evidence behind a claim; a nominally read-only health check can also perform signing work | Persist the first canonical verified report against the exact policy and ordered result hashes, atomically recheck results before its immutable insert, and make health verify only the stored bytes | Schema v1 migrates transactionally; completed work without a report degrades; the first explicit summary signs once; repeats return the exact stored report without signing; and missing, altered, stale, or conflicting reports remain fail-closed without regeneration |
| A crash after the last benchmark case leaves a complete campaign permanently without its final report | The case matrix has no claimable work, so later background ticks return idle and no caller remembers to invoke report signing; a naive repair could instead sign on every idle tick or persist after losing its host lease | Let the production benchmark runtime finalize only an exact complete matrix with no report, run automatically after the last case and on the next no-work recovery tick, and guard both sides of report construction with the outer lease | Normal completion stores the report in the same runtime path; a simulated post-case crash is recovered on the next idle tick; lease loss after signing leaves no report row; and an existing or invalid stored report is never automatically replaced or re-signed |
| An HTTP reader exposes another campaign or treats an incomplete report as evidence | A valid reader credential is reused across campaign IDs, progress counts are accepted as proof, report retrieval signs or repairs state, or adapter exceptions disclose private benchmark content | Bind the authenticated principal to the exact configured campaign, validate content-free status before loading, expose only the existing reverified signed report and its canonical digest, require HTTPS with an empty query-free GET, and reduce all failures to stable codes | Exact status and report reads succeed without signing or writes; wrong campaigns, incomplete work, malformed runtime output, plaintext, bodies, queries, invalid principals, and private exceptions fail closed before report delivery |
| A remote benchmark reviewer learns output origins, receives the source during the native pass, or returns an unrelated verdict | A generic HTTP integration follows redirects with credentials, merges both passes, accepts a response for another blind assignment, or retries outside the durable campaign | Send one exact phase-specific anonymous request to one fixed HTTPS endpoint with redirects disabled; forbid source fields in `target_native`; bind canonical request digest and deterministic review ID in body and headers; validate the complete response before storage; leave retries to campaign leases | Transport tests prove two separate calls, source blindness, absence of identity keys, exact idempotency and digest headers, strict endpoints and authentication, bounded statuses and bodies, duplicate/BOM rejection, mutation detection, cross-module contracts, wrong bindings, and invalid preferred-defect rejection |
| The production runtime has a provider interface but no concrete safe transport | A deployment invents incompatible envelopes, leaks credentials on redirects, retries inside the adapter, or cannot connect a separately tested older adapter to the current worker schema and durable queue | Integrate the reviewed request-bound HTTPS transport, bind it to the current worker contract, accept its structural content-free failure contract across dynamically loaded modules, and keep all retries in the queue | Unit tests retain the transport security cases; a disk-backed queue drives three ordered current-schema phase calls, preserves the source-blind native pass, and recovers one retryable HTTP failure without hidden adapter retries |
| Production quality evidence has only an abstract provider boundary | A deployment invents an incompatible review envelope, leaks credentials through a redirect, accepts evidence for another locale or result, retries outside the durable lease, or folds native Unicode before review | Provide one fixed-endpoint HTTPS adapter with host-injected authentication, no redirects, canonical native-Unicode request hashing, exact outer and inner bindings, strict bounded parsing, stable content-free failures, and one transport attempt per leased evidence job | Transport tests cover endpoints, authentication, native Unicode, request and response tampering, statuses, parsers, sizes, and redaction; a durable coordinator fixture proves one 503 attempt, queue-owned backoff, exact retry, receipt verification, signed approval, and full-locale delivery readiness |
| A valid quality receipt is replayed after release context or review purpose changes | The verifier sees only source, target, and locale, so changed policy, glossary, model, software, profile, result evidence, or a qualified-human escalation can reuse an old receipt | Give every verifier one canonical versioned binding containing the complete result and release-policy context plus an explicit quality, qualified-human, or independent-model purpose; require the trusted verifier to authenticate every field | Contract assertions cover all release dimensions; HMAC-bound regressions reject policy replay and prevent a quality receipt from satisfying qualified-human review before approval signing |
| Receipt verification is left as unsafe deployment glue or a temporary outage becomes permanent rejection | A host follows redirects with credentials, accepts an unrelated boolean, retries outside the durable lease, leaks verifier detail, or loses a valid locale because the release store erases retryability | Provide one fixed-endpoint HTTPS verifier with host-injected authentication, canonical binding/receipt identity, no redirects, strict bounded responses, one attempt, content-free structured failures, and propagation into the existing evidence backoff | Transport tests cover exact native-Unicode payloads, authentication, endpoints, idempotency, negative verdicts, status classes, malformed responses, and binding changes; an end-to-end Finnish release uses the concrete verifier, and a transient verifier outage enters durable retry without signing or publishing |
| The production runtime has durable CMS methods but no current authenticated ingress | A deployment accepts schema-v1 events without source ordering, returns before locale jobs persist, lets one valid tenant credential query another site's event, repeats work after a timeout, or exposes text through status and parser errors | Expose the runtime's exact bridge through a v2-only HTTPS WSGI API; sign canonical change and short-lived status requests; bind status to the stored site and original key ID; reuse durable event, sequence, plan, and job identities; revalidate successful results; return only content-free state | API and runtime tests cover 23-locale intake, exact replay, ID/sequence collisions, delayed supersession, site/key isolation, malformed transport, freshness, expired leases, hashed errors, queue tampering, cross-module signatures, and zero-schema invalid runtime configuration |
| Service health exists only as an in-process method or is exposed through tenant credentials | An unauthenticated probe enumerates site identifiers, a blocked state returns HTTP 200 as healthy, malformed monitor output leaks prose, or a provider probe is configured without an operator boundary | Add an explicitly configured service-operator HTTPS reader; authenticate before monitor access; reject bodies, queries and ambiguous headers; revalidate the complete content-free report; return 503 for blocked state; reject partial configuration before schema writes | HTTP and runtime tests prove exact operator scope, authentication-before-state ordering, healthy/degraded/blocked semantics, output-schema rejection, prose redaction, disabled-by-default exposure, and zero-schema invalid configuration |
| A CMS cannot safely withdraw obsolete localization work without inventing replacement text | A stale draft continues through paid model and review calls, another tenant key cancels it, an outbox race publishes after cancellation, or a caller assumes an already accepted publication was retracted | Accept one immutable signed cancellation bound to the exact event, site, source, source sequence, website version, and original credential; exclude it from service scheduling; close only non-leased unpublished outbox work; reverify the ledger in status and health | First cancellation and exact replay are idempotent; altered or cross-key bindings, ledger tampering, ID collisions, in-flight and completed deliveries block; pending work makes no model, review, or publisher call; schema v2 migrates transactionally |
| A cancellation cannot reach an event stranded before queue insertion | The signed event is durable as `accepted`, but cancellation requires `enqueued`; replay later creates paid work despite the CMS having withdrawn the revision, or health treats the intentional withdrawal as an outage | Permit the exact signed cancellation against both intake states, recheck its immutable ledger before and after queue insertion, keep cancelled accepted events out of the service, and synthesize content-free per-locale cancellation state without queue rows | A simulated pre-queue outage can be cancelled and replay cannot add jobs; cancellation during insertion never promotes the event; status, lifecycle, release blocking, and health all remain fail-closed and provider-free |
| A CMS deletion removes the wrong published revision or is falsely reported complete | Cancellation is reused after publication, a crash repeats an unbound deletion, the locale set drifts, or a forged acknowledgement marks content deleted | Accept a distinct signed tombstone only for the exact acknowledged publication; bind its original tenant key, event, source generation, publication ID and hash, plan, version, and complete locale set; deliver a separately signed content-free payload through a leased outbox and require an exact signed `deleted` acknowledgement | Unpublished, altered, cross-key, colliding, and tampered requests block; exact replay is idempotent; lease recovery and bounded retries preserve one identity; lifecycle and health distinguish deleting, failed deletion, and deletion without a model call or loss of publication history |
| A signed CMS change remains stranded after a crash before queue insertion | The API durably records `accepted`, the response is lost, and the CMS never replays it, so no locale job is ever created although health only reports the stalled intake | Let the supervised service reverify and resume one stored accepted event per tick, use the API's exact attempt ceiling, reuse deterministic plan/job IDs, and recheck cancellation around the idempotent queue transaction | A simulated pre-queue crash recovers without replay or a model call; concurrent completion is a no-op, configured attempts survive, cancellation never revives, and stored-event tampering blocks before other work |
| A durably accepted CMS change looks missing before its queue recovery tick | Tenant status rejects the event as not enqueued, while a synthetic shortcut could expose source text, invent queue rows, mutate state during a read, or override a withdrawal | Reverify the stored event, original credential, site, plan, and cancellation ledger; expose a distinct content-free `queue_recovery` state with zero real queue counts and let only the supervised service resume it | Signed status and lifecycle reads show every required locale and one stable reason without text or writes; cancellation retains priority; the next supervised tick replaces the synthetic view with real queue state |
| A CMS discovers locales but must guess the matching HTTP contract | Hand-copied paths or schemas drift from routing, a host advertises disabled lifecycle operations, or a digest omits fields and lets incompatible clients proceed | Build one content-free operation manifest directly from the active router and verified bridge schemas, include exact availability, and hash every canonical field independently of the locale registry | Authenticated discovery covers all six operations; schema/path changes alter the digest; standalone deployments mark lifecycle and tombstone disabled; the composed runtime advertises every operation enabled without state writes |
| A CMS discovers the inbound API but must guess the outbound publication callback | Documentation drifts from the built-in adapter, a receiver acknowledges the wrong envelope, required idempotency bindings are omitted, or an advertised endpoint leaks deployment data | Publish a separately hashed, content-free adapter contract from the same schema and header constants used by the transport; describe both publication and tombstone acknowledgements and at-least-once delivery without exposing an endpoint or credential | Capability and transport tests pin both delivery operations, exact request/response/payload/acknowledgement schemas, status values, content types, and all three binding headers; corrupt contract constants block the complete discovery response |
| A configured CMS callback is advertised but unavailable or bound to a different contract | Queue health remains green until the first complete website waits on a failing publication; a second green endpoint, generic ping, redirect with credentials, or unsigned response can mask the real receiver | Require the probe to be the exact delivery-publisher capability, add an optional content-free challenge to the built-in HTTPS publisher, bind its random probe ID and advertised contract digest in headers and signed acknowledgement, and include the result as a blocking health component | Exact signed health succeeds with no website content; a second probe, wrong contract, replayed probe, invalid signature, redirect, network and parser failures block with stable reasons; deployments without the optional probe remain compatible |
| A CMS receives approved commercial text but cannot verify which offer profile and review evidence reached publication | The internal release gate validates pricing fidelity, then the callback reduces that decision to an opaque approval ID; profile drift or missing evidence is invisible to the receiver unless it reparses multilingual prices heuristically | Carry one content-free release-evidence object per locale inside the signed publication, binding approval, result, and quality-receipt hashes plus the exact commercial profile and validated review summary; reject malformed evidence in outbox recovery, lifecycle, and health | Ordinary and commercial deliveries expose exact hash bindings without source, target, amount, tax, brand, or reviewer prose; altered or incomplete evidence blocks before the publisher and marks health fail-closed |
| Each CMS reimplements the signed publication callback or acknowledges before its durable write | A valid signature masks a partial locale bundle, stale source generation, wrong commercial profile, altered binding header, or an uncommitted write; at-least-once retry then reports content published when the CMS never atomically accepted it | Provide one strict reference receiver that compares the complete signed bundle with a host-supplied expectation, verifies all release evidence and cryptographic bindings before host code, requires an exact idempotent commit receipt, and signs the acknowledgement only afterward | Sender-to-receiver tests cover exact replay and successful commit; malformed transport, tampering, missing locales, wrong source or profile, expiry, private commit failure, wrong receipt, and acknowledgement-signing failure never produce a false acceptance |
| A CMS implements publication receipt verification but handles tombstone callbacks as ordinary deletion requests | A validly signed tombstone removes another publication generation, only part of its locale bundle, or nothing at all before returning `deleted`; replay then hides the inconsistency | Extend the strict reference receiver with a host-supplied tombstone expectation bound to the acknowledged publication ID and hash, source generation, and complete locale set; require an exact atomic, idempotent delete receipt before signing `deleted` | End-to-end sender and receiver tests cover successful deletion and exact replay; wrong publication bindings, source generation, locales, hashes, headers, signatures, private deletion errors, and false receipts never produce a deletion acknowledgement |
| A CMS implements the publication receiver but answers health from a weaker or unrelated path | An unauthenticated replay, wrong callback-contract digest, generic process ping, private host error, or premature signature makes the publisher look available although real callbacks cannot be accepted | Extend the same strict reference receiver with canonical content-free parsing, host-owned authentication, exact probe and contract binding, an exact host health receipt, and acknowledgement signing only after confirmation | Sender-to-receiver health succeeds without customer content; malformed probes and headers never authenticate, failed authentication never checks host state, wrong contracts and false receipts block, and private host or signing failures remain stable and retryable |
| A CMS has verified callback functions but must rebuild the public HTTP boundary | Hand-written routing accepts plaintext, queries, ambiguous framing, or an unknown operation; an unauthenticated request triggers tenant lookup; or private resolver detail enters an HTTP response | Compose publication, tombstone, and health on the publisher's one configured URL with an HTTPS-only WSGI boundary; authenticate before JSON dispatch, allow only the three exact schemas, verify signed deliveries before resolving current host expectations, and emit one content-free error envelope | One real publisher adapter completes all three operations through the WSGI callable; path, method, TLS, query, media type, framing, length, authentication, schema, signature, resolver, stale expectation, truncation, and size regressions block before the corresponding host effect |
| The reference receiver delegates atomicity to deployment glue that loses the last-known-good bundle or races a source change | A crash stores only some locales, a retry reactivates deleted prose, an older verified callback commits after a newer source registration, or health stays green after SQLite state is altered | Provide a dedicated SQLite host store; register monotonic source and exact tombstone expectations; recheck all bindings under `BEGIN IMMEDIATE`; preserve the prior active bundle until full replacement; scrub prose only in the atomic delete; verify schema, hashes, locale rows, active pointers, and tombstone state during health | Real WSGI sender-to-store tests cover publish/replay/delete/replay/health; restart keeps idempotency; source races and unregistered tombstones block; injected write failure rolls back to last-known-good; schema and content tampering cannot report healthy |
| Deployment glue wires the durable receiver with the wrong authority, callback hash, connection lifetime, or weaker health path | Invalid configuration creates a database before failing, one key signs and verifies both trust domains, a forked or threaded worker shares SQLite state, or only some store callbacks reach the public receiver | Provide one composition root that preflights every receiver argument and authority separation before opening SQLite, owns one connection and all exact store callbacks, rejects URI flags, and documents construction after each worker starts | Invalid-configuration tests create no database; a real sender persists and replays through a worker restart; schema failure returns no partial runtime; close is idempotent and later trusted operations block |
| A multithreaded WSGI worker enters one receiver SQLite connection concurrently | Two identical retries overlap transactions, health or rendering reads race a write, an exception strands a lock, or disabling SQLite's thread check merely turns a clear failure into corrupt state | Put every resolver, write, health check, trusted registration, and rendering read behind one worker-owned reentrant lock; enable cross-thread SQLite access only inside that wrapper; continue to forbid constructing the runtime before a process fork | Twenty-four concurrent signed retries converge on one receipt and bundle; a failed locked operation releases for a later authenticated health check; the full suite remains green on both supported Python versions |
| A pre-fork server inherits a CMS runtime into another process | The child reuses a duplicated SQLite connection or waits forever on a lock held by a vanished parent thread, while retries appear to be ordinary receiver failures | Bind the runtime to its creator process and check ownership before every lock acquisition; block inherited trusted operations and callbacks without exposing content, while allowing each post-fork worker to open its own connection | Simulated process drift blocks reads, writes, close, and signed WSGI publication before store access; two independent worker runtimes on one database handle concurrent retries and converge on one receipt and bundle |
| Replaced CMS bundles retain superseded target prose indefinitely | Old localized pages and release evidence remain readable in SQLite after a newer complete bundle is active, increasing breach impact and storage without serving rollback | Enable SQLite secure deletion and scrub the predecessor payload and locale rows only after the replacement is complete and the active pointer has moved, all inside the same transaction; retain only content-free replay bindings | A successful replacement leaves prose only for the active generation; a cleanup trigger failure rolls the entire switch back to the last-known-good bundle; restored superseded content makes health fail closed |
| A source dispatch succeeds but its website never learns the terminal localization outcome | A poller replays an expired signed read, accepts another plan or source generation, overlaps after a crash, or retries an outage without limit while its status leaks website prose | Register only an exact successful durable change dispatch; bind event, site, plan, version, sequence, job count, and canonical change hash; create a fresh signed lifecycle request per leased poll; separate normal polling from capped consecutive-failure backoff; retain only content-free state | Real client integration proves distinct request IDs across polls; cross-connection leases recover crashes; altered generations, responses, schemas, and state block; retry ceilings and terminal remote failures remain visible without retaining source or target text |
| A source CMS successfully submits a website change but never begins lifecycle monitoring | The process exits after the remote acknowledgement or after the separate monitor commit; a manual host handoff is forgotten, normal changes starve removals, or recovery sends duplicate logical work | Compose removal dispatch, acknowledgement-to-monitor reconciliation, change dispatch, and lifecycle polling into one removal-first source service; reuse exact persisted IDs, acknowledgements, hashes, leases, and idempotent registration while permitting only one network operation per tick | Tests cover both crash boundaries, restart recovery without change resend, removal priority, conflicting bindings before network access, stable error redaction, content-free health, bounded loop delays, and invalid configuration before schema creation |

| The source-CMS service has durable logic but no owned production runtime | A bad worker, timeout, lease, or path creates partial state; threads enter one connection concurrently; a pre-fork child inherits a locked runtime; or linked, shared, replaced, or permission-weakened databases allow the next request to use untrusted state | Add one composition root that validates the complete service in memory, requires three distinct canonical private database files, owns their connections, serializes threads, binds itself to its creator process, and rechecks every file identity around each operation | Invalid configuration and unsafe paths create no schemas; restart, close, fork, permission drift, 24 concurrent ticks, and two separately opened workers prove fail-closed operation and one durable change dispatch |
| The source-CMS runtime has no authenticated deployment ingress | A local bridge writes unverified bodies, accepts ambiguous HTTP framing, leaks source text through errors or health, shares one credential across write and read paths, or treats an identity collision as a successful replay | Add an HTTPS-only WSGI boundary with bounded exact bodies, content-free hash-bound authentication, separate route scopes, strict schemas, durable enqueue-before-response, and whitelisted output fields | End-to-end change and removal flows, health, identical replay, conflicting replay, malformed framing, wrong scopes, private exceptions, database tampering, and concurrent requests all remain content-free and fail closed |
| A source CMS can submit work but cannot safely read its later outcome | Deployment glue queries SQLite directly, exposes another site's event, leaks stored website prose, accepts a corrupt dispatch-to-lifecycle binding, or mutates worker state while serving status | Project one exact stored event through a strictly read-only service and runtime method; bind a dedicated status principal to the requested site; whitelist generation IDs, hashes, states, counters, locales, and stable reasons only | Tests follow queued work through remote lifecycle observation without extra network calls, make missing and cross-site events indistinguishable, reject an unbound runtime response, and redact private failures |
| Source-CMS integrators must reconstruct the active HTTP contract from prose | Generated clients use a stale path, shared scope, wrong principal or schema, overlook a transport bound, or trust a partial capability response while the runtime routes something else | Publish one separately authenticated, content-free capability object derived from the active routing constants; bind every operation, limit, required top-level field, and success status under a canonical digest; block the complete response on drift | Tests compare every advertised operation with live WSGI method and scope maps, verify the digest and body-free authentication, prove no runtime call occurs, and make altered route metadata fail closed |
| A source-CMS HTTP host accepts work while no dispatcher is alive | Accepted changes remain indefinitely queued after forgotten worker startup, a sleeping worker delays shutdown, a provider call makes shutdown unbounded, or a crashed thread leaves the process apparently healthy | Add an owned non-daemon worker lifecycle with interruptible waits, signal-before-join shutdown, explicit fail-closed readiness, managed-write gating, and stable worker failure codes | Tests prove automatic dispatch, readiness transitions, write rejection after stop, bounded shutdown during a blocked provider call, private-error redaction, invalid startup rejection, and close-before-database teardown |
| A source CMS observes a terminal result but its website backend never receives the outcome | A crash loses the handoff after the terminal monitor commit, a lost response causes duplicate effects, altered generation evidence reaches another site, retries never stop, or callback errors leak customer prose | Derive one content-free notification from the verified terminal lifecycle, persist its deterministic identity before host code, lease bounded attempts, and require an exact acknowledgement bound to event, site, and payload hash | Restart repairs the registration gap; a lost response reuses one identity; terminal success and failure remain distinguishable; collisions, altered evidence, invalid acknowledgements, exhausted retries, lease expiry, and state tampering block health before another callback |
| A durable terminal notification crosses an unsafe or ambiguous HTTP boundary | A redirect forwards credentials, an HTTP client silently retries, authentication signs different bytes, a valid acknowledgement belongs to another site or generation, or a private gateway response enters durable status | Pin one HTTPS endpoint, disable redirects and transport retries, authenticate the exact canonical body hash plus notification identity, reserve idempotency/binding headers, and require a byte-bound exact acknowledgement | Transport tests prove one attempt, fixed origin, exact body/auth/header hashes, stable retry classification, outbox-owned replay, and rejection of insecure endpoints, injected headers, malformed JSON, duplicate keys, wrong content type, altered notification identity, and cross-bound acknowledgements |
| A durable terminal receiver acknowledges work while its CMS processor is absent or dying | A forgotten worker leaves accepted notifications stranded, a callback races database shutdown, an idle loop delays termination, a private loop exception is leaked, or a pre-fork child controls the parent's thread and connection | Add one process-owned non-daemon worker, gate managed HTTP intake on running state and verified inbox health, use interruptible state-specific waits, redact failures to stable codes, and join before closing SQLite | Tests prove automatic receipt-to-processing, readiness transitions, rejection without a live worker, prompt idle shutdown, database preservation on join timeout, private-error redaction, invalid configuration before file creation, and foreign-process blocking |
| A CMS operator cannot safely observe terminal-receiver processing over the public boundary | Deployment glue reads SQLite directly, a status lookup exposes another site's event, a reused write credential gains operational access, readiness stays green after worker or storage failure, or a read mutates a lease | Add canonical site-bound status and body-free readiness routes with distinct read scopes, exact body-hash authentication, whitelisted content-free responses, and no processing operations | Tests cover pending-to-succeeded status, foreign-site equivalence with missing events, read-only storage counters, managed readiness transitions, scope separation, transport and hash rejection, and private-error redaction |
| Terminal-receiver integrators must reconstruct the active HTTP contract from prose | A generated client uses a stale path or schema, reuses a write credential for discovery, omits a binding field, or advertises a custom intake path that collides with an operational route | Publish one separately authenticated, body-free capability object derived from the active receiver constants and configured intake path; hash every canonical field and reject the complete contract on drift | Tests compare all four operations with runtime routes and scopes, verify the digest and empty-body authentication, prove no inbox or worker call occurs, preserve custom paths, and make schema drift or path collisions fail closed |
| Source delivery uses a fixed callback adapter beside the pinned receiver client | A custom path changes without the writer, credential code replaces an idempotency header, malformed terminal evidence reaches the network, an acknowledgement belongs to another tenant, or nested retries duplicate side effects | Make the contract-pinned client callable as the durable notifier; validate the immutable notification before discovery, take the write path only from the freshly verified contract, bind authentication and reserved headers to the exact payload, require an exact acknowledgement, and expose retryability without retrying internally | End-to-end WSGI tests prove custom-path discovery then one write, exact authentication and hashes, durable receipt, pre-network payload rejection, pre-write contract-drift blocking, cross-site acknowledgement rejection, stable retry metadata, and no hidden transport replay |
| Website backends must hand-build requests for the source-CMS ingress | A stale route accepts a different schema, a deployment swaps after discovery, an idempotency identity is reused with altered content, a response belongs to another site, or client retries multiply a write | Provide one provider-neutral, contract-pinned HTTPS client; validate immutable payloads before discovery, derive each operation from the freshly verified contract, bind every operational response to its capability digest and request identity, and leave bounded retries to the host | End-to-end WSGI tests cover all write and read operations, exact auth and body hashes, idempotent replay, pre-write contract drift, payload and tenant tampering, redirects, stable retryability, and content-free status data |
| A website process calls the one-attempt source client without a durable handoff | A crash before the request loses a source change; a crash after remote acceptance causes uncertainty; an old lease overwrites a newer result; or nested retry policies multiply writes and hide exhausted work | Persist one immutable change, cancellation, or tombstone before network access; bind its capability digest and two separate retry ceilings; claim one token-bound attempt; replay the same idempotency identity after lease expiry; prioritize removals; and block health on tampering, drift, expiry, or terminal failure | Tests prove persist-before-call, exact replay, removal priority, durable backoff, bounded permanent failure, crash recovery after remote acceptance, stale-lease rejection, capability drift, short-lease rejection, and no client call after database tampering |
| The website-source outbox is durable but deployment glue owns its connection and worker lifetime | Invalid configuration creates a database before failing; concurrent threads overlap transactions; a prefork child inherits a locked connection; linked or replaced storage receives trusted work; a dead worker still accepts events; or shutdown closes SQLite beneath a slow provider call | Add one preflighted composition root with an owner-only identity-guarded file, process binding before every lock, serialized connection access, a supervised non-daemon worker, managed-intake gating, content-free readiness, and signal-before-join shutdown that preserves the open database on timeout | Tests cover no-file invalid preflight, mode `0600`, restart recovery, automatic delivery, stopped-worker intake rejection, permission drift, links, inode replacement, process drift, 24 concurrent enqueues, redacted worker failure, bounded blocked-provider shutdown, and shared durable claims |
| Non-Python website backends cannot use the protected source-delivery runtime without custom deployment glue | A local endpoint parses hostile content before authentication, accepts another site's event, leaks source text through status, queues work behind a dead worker, rehashes a weakened contract, or serializes a forged runtime result | Add one authenticated HTTPS/WSGI sidecar with route-specific scopes, exact body and payload hashes, tenant-bound writes and reads, managed-worker readiness, independently validated content-free responses, and a canonical contract digest | End-to-end tests cover change, cancellation and tombstone intake through remote delivery, exact authentication and idempotency bindings, cross-site indistinguishability, stopped workers, damaged storage, malformed framing, contract drift, response substitution, private failures, and concurrent replay |
| Website and CMS backends must hand-build requests for the source-delivery sidecar | A stale route is used after discovery, credential code overwrites binding headers, sidecar and downstream contracts are confused, a response substitutes another tenant or payload, or hidden redirects and retries duplicate work | Provide one provider-neutral HTTPS reference client with fresh exact capability validation, separate sidecar and downstream pins, reserved headers, complete known status bindings, one transport attempt, and stable retryability | End-to-end WSGI tests cover all six routes, changes, cancellations, tombstones, exact inner-payload hashes, stale and downstream contract pins, response tampering, cross-site status, reserved-header injection, redirects, timeouts, malformed input, and parallel idempotent replay |
| Every source-delivery deployment must invent authentication around the public callback contract | A signature authenticates only the body but not its route or tenant, a captured proof is replayed, an old credential survives rotation, another site's scope is accepted, altered replay state is trusted, or a failure leaks a key or customer content | Supply a provider-neutral HMAC signer and verifier that bind origin, route, scope, body, tenant, idempotency, payload, and both capability pins; atomically consume random nonces in a durable content-free SQLite ledger; allow only explicit credential generations and scopes | End-to-end tests cover all six routes, exact proof fields, replay, expiry and future skew, wrong keys and pins, retired generations, tenant and scope separation, altered bindings, replay-ledger tampering, clock failure, redaction, and concurrent idempotent writes |
| The HMAC contract is correct but its caller-owned replay connection is deployed unsafely | The replay database is shared or replaced, permissions weaken, a forked worker inherits its connection, shutdown closes authentication beneath a live request, invalid configuration leaves operational state, or auth failure is invisible beside outbox health | Add one preflighted composition root with separate owner-only databases, safe-parent and inode guards, process binding before locks, serialized replay access, content-free auth health, and delivery-before-auth shutdown ordering | Tests cover no-file invalid preflight, mode `0600`, end-to-end hosted requests, durable replay after restart, permission drift, links, inode replacement, process drift, closed state, corrupt replay rows, parallel idempotent writes, redacted health, and blocked-worker shutdown |
| CMS clients know that locale-specific commercial rendering exists but cannot retrieve the exact rules they must pin | A client guesses separators or currency placement, accepts a partial locale set, or silently follows tampered CLDR metadata while the server still advertises healthy capabilities | Publish one canonical content-free registry for all 24 locale rendering references, bind every entry to its commercial quality-profile version and digest, hash the complete registry, and validate exact canonical equality before returning signed capabilities | Capability tests require the exact 24-locale registry, CLDR 48 Maltese/Finnish/Austrian/Portuguese references, detached payloads, no project content, and fail-closed rejection of missing, reordered, or altered entries |
| The server publishes a complete commercial rendering registry but the source-side reference client accepts a self-rehashed substitute or silently crosses a deployment upgrade | A proxy or incompatible service replaces the registry and recomputes its public hashes, or a legitimate profile change reaches production before the CMS host deliberately accepts new rules | Make the reference client validate the exact v5 capability shape and canonical 24-locale registry against its installed contract, then support immutable overall-capability and rendering-registry deployment pins with distinct non-retryable failures | End-to-end client tests accept exact current pins and reject malformed pins before transport, wrong deployment pins after one response, plus missing, reordered, altered, or obsolete registries even when every public hash is recomputed |
| Deployment configures both commercial capability pins but the durable CMS source runtime never exercises them before accepting work | A host assumes constructor pins are active, opens persistent queues without a successful capability read, or the client binding changes after startup while queued website content continues toward the wrong contract | Add an explicit startup preflight that requires and verifies both immutable pins before creating any database, retains only their content-free hashes, and rechecks the client binding before every runtime transition | Runtime tests prove successful pinned startup, zero database creation for missing pins or remote mismatch, one bounded capability request, content-free status, and blocking before persistence or delivery when the client binding changes |
| A pinned CMS runtime restarts over durable queues that were created for another commercial capability generation or database role | Pending work silently crosses a price/offer policy deployment, swapped change/removal files acquire new tables, an unbound legacy queue with work is adopted, or a partially written/tampered binding is trusted after a crash | Persist the exact two-pin binding and database role independently in all three SQLite files, precheck existing metadata before service schema writes, adopt only provably empty unbound stores, and revalidate canonical rows on every operation | Restart tests preserve same-binding work, reject a coherent different binding and swapped roles before schema mutation, reject non-empty unbound stores, safely adopt empty legacy stores, recover same-binding partial writes, and block altered rows before queue access |
| The outer website outbox remains unbound while the sidecar and source queues pin a commercial generation | Website retries can resume under a different runtime or price-format policy, a stale sidecar can be trusted, or the website database is created before any authenticated proof of the downstream generation | Require both host-owned generation pins together, verify them through authenticated sidecar source readiness before opening SQLite, include them in the adapter contract, and persist the exact role-specific binding in the outer outbox | Tests prove exact signed preflight and persisted hashes, reject mismatch or outage before database creation, bind adapter-digest changes, preserve direct unpinned compatibility, and keep local status reads offline |
| A website runtime contacts the sidecar before discovering that its existing outer outbox is locally unsafe or belongs to another generation | A restart leaks operational traffic, consumes authentication nonces, or depends on an unavailable network even though a changed pin, tampered schema, unsafe file, or non-empty unbound queue already makes safe recovery impossible | Validate the existing file identity, base schema, canonical generation row, and derived digest through a read-only local preflight before the authenticated sidecar request; preserve remote-first creation and migration for missing or provably empty stores | Restart tests require zero transport calls for generation mismatch, schema tampering, unsafe permissions, and non-empty unbound work; same-generation state resumes, and an empty legacy store binds only after the unchanged authenticated preflight |
| The outer website generation is durable but invisible in composed operational projections | Operators see healthy website, sidecar, and source states without proof that the website outbox is still bound to the generation that accepted its work; a cached or reconstructed hash can hide database tampering | Project one canonical, content-free website capability binding directly from the guarded SQLite row and carry it independently through status, lifecycle, health, and readiness contracts before any downstream read | Exact binding and defensive-copy tests cover every projection; altered generation metadata blocks health and readiness locally with zero additional transport calls; payloads contain no endpoint, credential, tenant, source, or target text |
| The owned website runtime has many safe methods but no single discoverable end-to-end contract | An integrator pins only the sidecar, assumes stale projection schemas, collapses the three retry budgets, or treats durable source acceptance as publication; local generation drift is found only after an operational call | Publish one canonical content-free capability snapshot that binds every outer operation and projection schema, retry ownership and publication semantics to the guarded website generation plus freshly verified sidecar and source pins | Exact snapshot and defensive-copy tests prove deterministic hashing; local generation tampering blocks before network, and remote contract substitution blocks without returning partial capabilities or private data |

No heuristic is allowed to claim that it proves native fluency. Cryptographic proof covers process integrity, not linguistic truth.
