# Version 4 premortem

Assume BLUN Language Guard v4 shipped and failed in production.

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

No heuristic is allowed to claim that it proves native fluency. Cryptographic proof covers process integrity, not linguistic truth.
