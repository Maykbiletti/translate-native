# Website localization job planning

`integrations/website_localization.py` is the provider-neutral entry point for
the website-localization pipeline. It does not contact an LLM and cannot
publish content. Its only job is to turn one trusted source object into one
independently retryable queue payload per EU target locale.

## Premortem

Assume the planner shipped and failed: stale work was reused after a source,
glossary, policy, provider, model, or runtime change; the source language was
translated back into itself; or one queue entry mixed several target
languages. The early warning is a repeated job identity despite one changed
input or a job count that differs from 23 for an EU source and 24 for a non-EU
source. The mitigation is one immutable job per locale whose canonical
idempotency binding includes every result-affecting field and the exact source
hash. The regression suite mutates each binding separately and proves the
expected 24-profile registry and per-language job counts.

The current language registry follows the European Union's official list of 24
languages: <https://european-union.europa.eu/principles-countries-history/languages_en>.
Each language has one explicit BCP-47 website profile. The default German
profile is `de-AT`; English is `en-IE`, Portuguese is `pt-PT`, Spanish is
`es-ES`, and Swedish is `sv-SE`. A source whose primary language is already in
the registry produces 23 jobs. A non-EU source produces all 24.

## JSON contract

```bash
python3 integrations/website_localization.py <<'JSON'
{
  "source_id": "homepage.hero",
  "source_revision": "cms-184",
  "source_text": "Build your business with BLUN.",
  "source_locale": "en-IE",
  "content_type": "headline",
  "glossary_version": "blun-glossary-3",
  "policy_version": "native-web-1",
  "provider_id": "customer-llm",
  "model_id": "king",
  "model_version": "2026-08-29",
  "software_version": "6.43.0-dev",
  "target_locales": ["de-AT", "sv-SE"]
}
JSON
```

Omit `target_locales` to request every eligible EU language. An explicit list
must contain supported profiles, must not contain duplicates, and must exclude
the source language. Unknown input fields, ambiguous locale values such as
`auto`, non-NFC text, NUL characters, unsupported content types, oversized
source text, and wrong JSON types block the complete plan.

Every emitted job contains exactly one target profile, the unchanged source
text and its SHA-256 hash, both required quality-pass names, and
`release_required: true`. Its `job_id` and `idempotency_key` are identical and
are derived from canonical JSON bound to:

- source ID, revision, text hash, and locale;
- target locale and content type;
- glossary and quality-policy versions;
- provider, model ID, and model version;
- Translate Native software version.

Changing any bound value creates a new job and plan identity. A queue may
therefore deduplicate an exact retry, while stale work cannot silently survive
a changed source, glossary, policy, provider, model, or runtime. Later workers
must keep the two declared reviews separate—target-only native quality first,
source-aware fidelity second—and obtain a signed Translate Native release
before publication.
