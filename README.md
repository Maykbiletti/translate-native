<p align="center">
  <img src=".github/social-preview.png" alt="Translate Native — Meaning in. Native language out." width="100%">
</p>

<div align="center">

<pre>
 ____  _     _   _ _   _
| __ )| |   | | | | \ | |
|  _ \| |   | | | |  \| |
| |_) | |___| |_| | |\  |
|____/|_____|\___/|_| \_|
</pre>

# Translate Native

### Meaning in. Native language out. Release only after proof.

One universal agent skill for translations that sound written—not translated—and preserve every language's native script.

<p>
  <img alt="Tests" src="https://github.com/Maykbiletti/translate-native/actions/workflows/test.yml/badge.svg">
  <img alt="MIT License" src="https://img.shields.io/badge/license-MIT-7C3AED?style=flat-square">
  <img alt="Python 3" src="https://img.shields.io/badge/python-3.10%2B-3776AB?style=flat-square">
  <img alt="Dependencies" src="https://img.shields.io/badge/dependencies-zero-16A34A?style=flat-square">
  <img alt="Languages" src="https://img.shields.io/badge/languages-all-E11D48?style=flat-square">
</p>

**Built by BLUN · Skill + MCP + enforced release gate.**

</div>

---

> **Stop translating strings. Start rewriting meaning in the target language.**

Agents can speak beautifully with users in their own language and still produce stiff, literal copy as soon as the task is called “translation” or arrives inside an i18n file. Translate Native prevents that mode switch.

It reconstructs the meaning, discards the source sentence structure, and writes the message again with native syntax, rhythm, idiom, register, script, punctuation, and locale conventions—without changing the facts.

## One skill. Every human language.

The skill has no language allowlist. It applies equally to:

- Swedish, German, Czech, Spanish, Catalan, Basque, and every other Latin-script language;
- Chinese, Japanese, and Korean;
- Arabic, Persian, Urdu, and Hebrew;
- Greek, Cyrillic, Armenian, and Georgian writing systems;
- Indic, Southeast Asian, Indigenous, minority, endangered, and low-resource languages;
- regional standards, scripts, dialects, honorific systems, and specialist registers.

For uncertain and low-resource varieties, the rule is honesty: verify with community and authoritative sources, or request native review. Never fake fluency.

## What changes

| Ordinary translation mode | Translate Native |
| --- | --- |
| Mirrors source word order | Rebuilds native information flow |
| Chooses dictionary equivalents | Chooses native collocations |
| Produces generic “i18n language” | Writes for the real audience and medium |
| Flattens locale and script choices | Resolves locale, script, dialect, and register |
| Drops accents or romanizes native text | Preserves native spelling, diacritics, alphabets, scripts, and punctuation |
| Silently changes emphasis | Preserves claims, modality, negation, and uncertainty |
| Breaks variables during rewriting | Protects keys, placeholders, URLs, markup, and code |

## Two jobs. One mandatory skill.

Agents no longer need to activate a second orthography skill after translating. `translate-native` now owns both jobs in one self-contained release gate:

1. rewrite the meaning as natural original target-language prose;
2. enforce native spelling, diacritics, alphabets, scripts, punctuation, spacing, and Unicode.

The separate `native-diacritics` skill can still protect ordinary writing outside translation tasks. Inside a translation, its activation is optional because the full orthography contract is built directly into `translate-native`.

The root [`AGENTS.md`](AGENTS.md) tells repository-aware agents to load the combined workflow and treat every model-generated translation as an untrusted draft.

## Version 4: BLUN Language Guard

Version 4 turns the executable MCP gate into a signed, independently verifiable release system. The skill remains responsible for meaning, native rewriting, locale fit, and orthography. The server blocks deterministic defects and issues a cryptographic receipt bound to the exact source, target, locale, version, issue time, and expiry.

```text
Translation request
        ↓
translate-native skill
        ↓
Native pass → fidelity pass → integrity pass
        ↓
release_translation MCP tool
        ↓
BLOCK → revise and repeat
PASS  → signed receipt → verify receipt → deliver
```

The gate checks UTF-8/Unicode integrity, NFC normalization, script identity, balanced bidirectional isolates, dangerous overrides, frequent ASCII substitutions, optional terminology glossaries, and all seven mandatory attestations. Edited, expired, forged, wrong-locale, or wrong-version receipts fail verification.

### Install, update, and diagnose

```bash
python3 installer/blun_language_guard.py install
python3 installer/blun_language_guard.py doctor
python3 installer/blun_language_guard.py update
```

Installation uses atomic symlinks for Codex and Claude Code and refuses to overwrite existing non-symlink skill folders. It writes a mergeable MCP snippet but never overwrites a host configuration. Updates are cloned and tested before the active checkout is fast-forwarded. `doctor` runs the test suite and probes the live MCP tool list.

The portable fail-closed hook is [`pre_output_guard.py`](integrations/pre_output_guard.py). It accepts source, target, locale, and receipt as JSON on stdin and exits nonzero when verification fails. Host-specific adapters must pass the candidate output into this contract; a host hook that exposes no candidate text cannot enforce output validation.

### Supported structured formats

- JSON and ARB;
- HTML including linguistic metadata and JSON-LD linguistic fields while protecting schema, URLs, types, code, and placeholders;
- XML, Android resources, and structurally equivalent XLIFF documents;
- PO/POT catalogs;
- Apple `.strings`;
- SRT, VTT, and ASS subtitle timing;
- ICU placeholders and plural/select contracts inside supported containers.

See [`PREMORTEM.md`](docs/PREMORTEM.md) for the failure modes, mitigations, and proof required before calling this system production-ready.

No deterministic linter can prove that prose is genuinely native. That is why the MCP server supplements the skill's native-language judgment instead of pretending to replace it.

### Start the MCP server

The server has no third-party Python dependencies:

```bash
python3 translate-native/scripts/blun_language_guard.py serve
```

Copy [`mcp-config.example.json`](mcp-server/mcp-config.example.json), replace the absolute path, and merge the `blun-language-guard` entry into the MCP configuration used by your agent CLI.

Then copy the rules in [`AGENT_RULES.md`](integrations/AGENT_RULES.md) into the host's always-on instruction file:

- Codex: repository or global `AGENTS.md`;
- Claude Code: `CLAUDE.md`;
- another MCP-compatible agent: its equivalent persistent instruction file.

This combination matters. A skill can fail to trigger, and an MCP tool can remain unused. The persistent rule requires both the skill and the `release_translation` call.

### Validate from the terminal

The same engine can be used in hooks, CI, wrappers, or pre-publication scripts:

```bash
python3 translate-native/scripts/blun_language_guard.py validate --language sv-SE target.txt
```

Exit code `0` means the deterministic checks passed. Exit code `1` means delivery must stop. Semantic and native-quality review remains mandatory even after exit code `0`.

## Native examples

| Target | Native result |
| --- | --- |
| Swedish | `Om du redan har betalat behöver du inte göra något mer.` |
| Simplified Chinese | `你确认前，我们不会分享任何内容。` |
| Catalan | `No es compartirà res fins que ho confirmis.` |
| Basque | `Dagoeneko ordaindu baduzu, ez duzu beste ezer egin behar.` |
| Czech | `Pokud jste již zaplatili, nemusíte nic dalšího dělat.` |
| Spanish | `Si ya has pagado, no tienes que hacer nada más.` |

These are examples, not the boundary of the skill.

## Grammatical is not native

The skill now explicitly rejects agent-written copy that is grammatically correct but still sounds translated. Agents must inspect collocations, parallel structure, paragraph flow, register, unnecessary source-language borrowings, and vague AI filler—not just spelling and syntax.

The Swedish BLUN regression case captures the difference:

| Agent wording | Native review |
| --- | --- |
| `desktop- och mobilprogramvara` | `programvara för datorer och mobila enheter` |
| `fördela uppgiften på ett smart sätt` | `fördela arbetet mellan de modeller som passar bäst för uppgiften` |
| `fördela uppgiften till den modell som passar bäst` | `automatiskt välja den modell som passar bäst för uppgiften` |

All three agent formulations are understandable. They still fail because a native editor would recast them for publication. The complete candidates, defect analyses, native rewrites, and language-independent review procedure live in [`translationese-review.md`](translate-native/references/translationese-review.md).

## The translation contract

Every result passes seven gates:

1. **Meaning** — claims, relationships, conditions, and implications remain equivalent.
2. **Completeness** — nothing is lost, duplicated, or invented.
3. **Precision** — negation, modality, quantities, entities, and terminology remain exact.
4. **Nativeness** — calques, source syntax, and translationese are removed.
5. **Fit** — locale, script, register, audience, tone, and medium are right.
6. **Integrity** — placeholders, keys, links, markup, code, and structure survive intact.
7. **Orthography** — spelling, diacritics, punctuation, spacing, and Unicode are native.

Version 2 deliberately separates judgment into two passes: a target-only native edit that cannot lean on the source wording, followed by a source-aware fidelity audit that accounts for every claim and constraint. Publication-grade, long-form, and uncertain work can be routed through an independent defect review before release.

## i18n without i18n language

JSON, YAML, XML, PO, ARB, ICU MessageFormat, Android resources, Apple strings, Markdown, and HTML are containers. They do not excuse robotic prose.

```json
{
  "welcome": "Ongi etorri, {name}!",
  "upload": "Kargatu {{count}} fitxategi <strong>{project}</strong> proiektura.",
  "help": "Informazio gehiago: https://example.com/help"
}
```

The key structure stays fixed. The Basque values read naturally. Every placeholder, URL, and HTML tag remains protected.

## Install

Clone the repository:

```bash
git clone https://github.com/Maykbiletti/translate-native.git
```

Copy the `translate-native` directory into the skill directory used by your compatible agent platform, or load its [`SKILL.md`](translate-native/SKILL.md) according to that platform's skill instructions.

Invoke it explicitly:

```text
$translate-native
```

Direct skill address:

```text
https://github.com/Maykbiletti/translate-native/tree/main/translate-native
```

## Protect machine-readable content

The zero-dependency guard compares source and target files:

```bash
python3 translate-native/scripts/translation_guard.py source.md target.md
python3 translate-native/scripts/translation_guard.py source.json target.json --format json
python3 translate-native/scripts/translation_guard.py source.html target.html --format html
python3 translate-native/scripts/check_diacritics.py --language sv target.md
```

It blocks delivery when it detects:

- missing, added, or renamed placeholders;
- changed URLs, email addresses, code, HTML tags, escapes, or format tokens;
- changed JSON keys, hierarchy, arrays, scalar types, or non-string values;
- changed ICU argument names, formatters, selectors, or number signs;
- changed HTML structure, technical attributes, comments, scripts, or styles while still allowing native translation of linguistic accessibility attributes;
- target text that is not valid UTF-8 and Unicode NFC.

The guard protects structure. The agent's seven-pass review protects meaning and native quality.

The diacritics linter additionally catches frequent ASCII substitutions such as `schoen`, `forstar`, `informacion`, and `cestina` while leaving code, URLs, and other protected technical spans alone. It is a deterministic warning system, not a finite definition of any language; the built-in orthography gate remains mandatory for every script worldwide.

Language tags without a deterministic substitution list, such as `en`, are accepted. The linter still checks UTF-8 and Unicode NFC, prints that no language-specific diacritics rules apply, and leaves the mandatory native-orthography review in force.

For complete HTML pages, linguistic metadata may change: descriptions, keywords, application names, Open Graph titles/descriptions, and Twitter titles/descriptions are treated as translatable text while their placeholders remain protected. Technical metadata such as viewport settings, encodings, URLs, and unrelated `content` attributes remain fixed.

## Evidence, not dataset worship

| Source | Best use | Warning |
| --- | --- | --- |
| [Language communities and authorities](translate-native/references/native-translation-standard.md) | Orthography, terminology, accepted standard | Prefer the requested community's own convention |
| [Unicode CLDR](https://cldr.unicode.org/) | Locale IDs, formats, plurals, exemplar characters | Locale data is not prose guidance |
| [Unicode Normalization](https://unicode.org/reports/tr15/) | Canonical Unicode normalization | NFC does not prove correct spelling |
| [W3C Language Enablement](https://www.w3.org/International/typography/gap-analysis/language-matrix.html) | Script layout and typography | Web support data is not a dictionary |
| [IANA Language Subtag Registry](https://www.iana.org/assignments/language-subtag-registry) | Language, script, and region tags | Tags identify a target; they do not translate it |
| [Leipzig Corpora Collection](https://cls.corpora.uni-leipzig.de/) | Native usage and collocations | Check domain and date |
| [FLORES+](https://huggingface.co/datasets/openlanguagedata/flores_plus) | Multilingual evaluation | It is an evaluation set, not training data |
| [OPUS](https://opus.nlpl.eu/) | Supporting parallel examples | Large corpora can contain literal or noisy translations |

Hugging Face is a useful distribution platform, not a quality certificate. Provenance, curation, locale, license, domain, and intended use still matter.

## Repository structure

```text
translate-native/
├── SKILL.md
├── agents/
│   └── openai.yaml
├── assets/
│   └── icon.svg
├── references/
│   ├── native-translation-standard.md
│   ├── native-orthography.md
│   ├── evaluation-protocol.md
│   ├── translationese-review.md
│   └── structured-content.md
└── scripts/
    ├── translation_guard.py
    ├── check_diacritics.py
    └── blun_language_guard.py
```

The MCP configuration example, persistent CLI rules, automated tests, and GitHub Actions workflow live at repository level.

Machine-readable failure cases live in [`evals/regressions.jsonl`](evals/regressions.jsonl). They cover Swedish translationese and model selection, German umlauts, Spanish punctuation and accents, Czech and Catalan orthography, Vietnamese tone marks, Chinese and Arabic native scripts, and Ukrainian language identity. They are regression examples, never a language allowlist.

## Test

```bash
python3 -m unittest discover -s tests -v
```

The suite covers protected text, sentence punctuation after URLs, JSON structure and types, ICU plural/select syntax, safely translatable HTML attributes, HTML/code tampering, Unicode normalization, common missing diacritics in German, Spanish, and Czech, technical-span protection, the combined language-and-orthography contract, repository agent instructions, and both Swedish agent-copy regression cases.

## Design principle

Translation is not token replacement. It is constrained re-authorship: the same meaning, rebuilt inside another language's own system.

That standard is universal. Confidence is not.

## License

Released under the [MIT License](LICENSE).

---

<div align="center">

### Built for every language that people call home.

**BLUN**

*Get it done with BLUN.*

</div>
