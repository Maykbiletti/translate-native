# Version 5 premortem

Assume the Version 5 gateway and automatic updater shipped and failed in production.

| Failure | Early warning | Mitigation | Proof required |
| --- | --- | --- | --- |
| A receipt survives edited text | Verification accepts a changed source, target, locale, or expired receipt | Sign canonical hashes, version, issue time, expiry, and nonce with HMAC-SHA256 | Tampering and expiry tests fail closed |
| Agents invent receipt-shaped strings | A token passes based on its prefix or format | Verify the cryptographic signature and exact payload; never trust appearance | Forged-token regression test |
| Legitimate RTL text is blocked | Balanced isolates in Arabic or Hebrew fail | Block overrides/embeddings; allow only balanced isolates; flag unpaired controls | Balanced/unbalanced RTL tests |
| Script validation becomes a language allowlist | Unknown languages fail automatically | Apply script checks only when a requested script is known; otherwise report `not-evaluated` | Unknown-language clean pass |
| Format support is marketing only | A translated file passes after keys, timestamps, or placeholders change | Implement format-specific structural parsers and negative fixtures | One positive and multiple mutation tests per format |
| Glossaries reject normal inflection | Native grammatical forms are flagged as missing | Support exact, case-insensitive, and optional regex terms; keep glossary mode explicit | Inflection/regex fixtures |
| Installer damages existing configuration | Existing CLI settings disappear | Never overwrite host configs; install symlinks atomically and emit mergeable snippets | Idempotent install test |
| Auto-update installs a broken release | Update stops after replacing only part of the installation | Fetch, test, and validate before atomic symlink switch; preserve previous revision | Failed-update rollback test |
| Doctor reports false green | Files exist but MCP cannot initialize or issue/verify a receipt | Run live MCP initialize, tools/list, issue, verify, and tamper probes | Doctor integration test |
| Reviewer rubber-stamps its own output | Creation and judgment share the same context | Require target-only native review before source-aware fidelity review; record separate attestations | Contract test in SKILL.md |
| Agent simply skips the skill or MCP tool | Unreceipted text reaches a user | Intercept candidate output outside the agent and fail closed in the host adapter | End-to-end test proves raw output cannot escape |
| Agent steals the signing key | Agent and signer share one writable user or container | Run signer/gateway under a separate OS identity or remote service; deny agent filesystem and socket administration | Sandbox escape test cannot read key or replace executable |
| Auto-updater becomes a supply-chain path | An unreviewed remote commit installs silently | Test candidate before merge, permit trusted commit-signature enforcement, use fast-forward only, run post-install tests, retain rollback revision | Rejected unsigned and broken-update fixtures |
| Scheduler claims success but never runs | Update state timestamp stops advancing | `doctor` checks scheduler and update state age; operational alert on stale state | Forced scheduled run updates timestamp |
| Bad update passes tests but breaks hosts | Unit tests pass while CLI adapter fails | Canary rollout, post-update doctor, automatic rollback, phased release channel | Canary failure prevents stable rollout |
| Short SEO copy bypasses the 200-character profile | Titles pass despite folded spelling | Classify title/meta/UI content, return `REVIEW_REQUIRED`, and bind independent review to receipt | Real short-copy regressions cannot pass without review |
| CRLF is mistaken for changed content | Windows files differ only in size and raw hash | Maintain canonical text identity alongside byte identity; normalize BOM/newlines/NFC only | CRLF/BOM match while mojibake still fails |
| Caller approves its own review | `short_text_reviewed=true` releases damaged spelling | Treat caller fields as metadata only; measurable findings are unconditional; external reviewer owns real approval | Damaged text stays blocked with `reviewed=true` and `content_type=prose` |
| Correct German `ss` is mistaken for `ß` folding | `wissen`, `dass`, or `interessiert` blocks | Exclude `ss` from density heuristics; require lexical and locale context | Correct `ss` corpus passes while `ae/oe/ue` attack still blocks |
| A translation preserves tags but loses most text | Truncated target reports `structure intact` | Measure total linguistic units, segment count, and aligned segment coverage with script-aware thresholds | 71% omission exits nonzero while full and CJK controls pass |
| The CLI catches an omission but mandatory MCP release does not | A fully attested truncated target receives a token | Load the same auto-detected volume primitive inside `release_translation`; never trust a caller format switch | 71% omission stays blocked with all seven attestations set to true |

No heuristic is allowed to claim that it proves native fluency. Cryptographic proof covers process integrity, not linguistic truth.
