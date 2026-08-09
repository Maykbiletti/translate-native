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

### Meaning in. Native language out.

One universal agent skill for translations that sound written—not translated.

<p>
  <img alt="Tests" src="https://github.com/Maykbiletti/translate-native/actions/workflows/test.yml/badge.svg">
  <img alt="MIT License" src="https://img.shields.io/badge/license-MIT-7C3AED?style=flat-square">
  <img alt="Python 3" src="https://img.shields.io/badge/python-3.10%2B-3776AB?style=flat-square">
  <img alt="Dependencies" src="https://img.shields.io/badge/dependencies-zero-16A34A?style=flat-square">
  <img alt="Languages" src="https://img.shields.io/badge/languages-all-E11D48?style=flat-square">
</p>

**Built by BLUN · Get it done with BLUN.**

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
| Silently changes emphasis | Preserves claims, modality, negation, and uncertainty |
| Breaks variables during rewriting | Protects keys, placeholders, URLs, markup, and code |

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
```

It blocks delivery when it detects:

- missing, added, or renamed placeholders;
- changed URLs, email addresses, code, HTML tags, escapes, or format tokens;
- changed JSON keys, hierarchy, arrays, scalar types, or non-string values;
- changed ICU argument names, formatters, selectors, or number signs;
- changed HTML structure, technical attributes, comments, scripts, or styles while still allowing native translation of linguistic accessibility attributes;
- target text that is not valid UTF-8 and Unicode NFC.

The guard protects structure. The agent's seven-pass review protects meaning and native quality.

## Evidence, not dataset worship

| Source | Best use | Warning |
| --- | --- | --- |
| [Language communities and authorities](translate-native/references/native-translation-standard.md) | Orthography, terminology, accepted standard | Prefer the requested community's own convention |
| [Unicode CLDR](https://cldr.unicode.org/) | Locale IDs, formats, plurals, exemplar characters | Locale data is not prose guidance |
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
│   ├── evaluation-protocol.md
│   └── structured-content.md
└── scripts/
    └── translation_guard.py
```

Automated tests and the GitHub Actions workflow live at repository level.

## Test

```bash
python3 -m unittest discover -s tests -v
```

The suite covers protected text, sentence punctuation after URLs, JSON structure and types, ICU plural/select syntax, safely translatable HTML attributes, HTML/code tampering, Unicode normalization, and expected failure cases.

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
