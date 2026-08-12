# BLUN Code mandatory language release

## Security boundary

BLUN Code must treat model output as untrusted until the isolated Language Guard verifies the exact final envelope. The runtime must buffer model `text-delta` and `done` events, retain Telegram and HTTP credentials outside the agent process, and invoke a sender only with the verified `target_text`.

The only agent-owned final fields are:

```json
{
  "target_text": "Natürlich ist das möglich.",
  "release_token": "blg6.…"
}
```

BLUN Code owns the task kind, exact language, complete translation source, content type, agent identity, and delivery channel. Unknown fields or an attempt to return these host fields blocks before delivery.

## Runtime sequence

1. The installer writes the named MCP entry to `~/.blun/mcp.json`, creates the service token, and starts the isolated guard service.
2. BLUN Code imports that named entry once into its encrypted MCP store without replacing existing MCP servers.
3. The host creates a structured route. Ordinary chat is `response`; a translation requires `languageGuardTaskKind: translation`, `languageGuardSourceText`, and `languageGuardLanguage`.
4. BLUN Code adds a mandatory instruction to the model turn and suppresses candidate text events.
5. The agent applies `translate-native` for translations, calls the correct release tool, and returns the strict envelope.
6. BLUN Code asks the isolated service to verify the exact text, purpose, source, language, content policy, and receipt.
7. Only a valid result becomes a renderer delta, desktop result, Telegram message, or another external response.

Errors, cancellations, approvals, and host-generated status messages are not model prose and may remain visible, but they must never interpolate an unverified candidate.

## Trusted metadata

The adapter accepts these host-owned fields in `meta`:

- `languageGuardTaskKind`: `response` or `translation`;
- `languageGuardLanguage`: exact BCP 47 language or locale;
- `languageGuardSourceText`: complete independently captured source for translations;
- `languageGuardContentType`: `prose`, `title`, `meta_description`, or `ui`.

Telegram may use the sender's `language_code` as the response language when no explicit session language is set. Desktop sessions fall back to the configured BLUN interface language. A product should expose an explicit conversation language when interface language and response language can differ.

Free-form prompt inspection is not an authority boundary. No regex or model classifier can prove that arbitrary text is or is not a translation request. A translation workflow therefore needs a structured UI or API route that supplies the source and target language. Until that route exists, the adapter instructs an agent not to execute a translation through the response path; high-assurance deployments should block ambiguous free-form translation requests at intake.

## Deployment levels

- Per-user autostart protects against accidental secret inheritance and forgotten tools.
- A separate service account or container protects the key from a hostile agent running as the desktop user.
- A remote signer additionally separates the host machine, but must use authenticated transport, replay limits, bounded requests, and the same content-free audit policy.

The automatic repository updater refreshes the skill, MCP server, service, and portable adapters. BLUN Code itself follows its own signed application update channel; updating one repository does not silently rewrite the other.
