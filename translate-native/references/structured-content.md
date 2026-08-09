# Translating Structured Content

## General rule

Protect machine-readable structure first, then write human-visible values as naturally as free prose. A resource file is a delivery format, not a linguistic style.

Before editing, identify:

- translatable values and non-translatable identifiers;
- target locale and product glossary;
- placeholder syntax and runtime formatter;
- plural, gender, select, and fallback behavior;
- markup, links, escapes, accelerator keys, and length limits;
- context from screenshots, comments, neighboring entries, and call sites.

## Preserve by format

| Format | Translate | Preserve exactly |
| --- | --- | --- |
| JSON / ARB | User-visible string values and descriptions when intended | Keys, hierarchy, types, arrays, placeholders |
| YAML | User-visible scalar values | Keys, indentation semantics, anchors, tags, types |
| PO / POT | `msgstr`; contextual human copy | `msgid`, comments, flags, plural indices, format tokens |
| Android XML | Text nodes intended for users | Resource names, tags, escapes, `%` placeholders, quantity items |
| Apple strings | Values | Keys unless product rules say otherwise, format specifiers, escapes |
| ICU MessageFormat | Text within every branch | Argument names, selector keys, braces, plural/select structure |
| HTML / JSX | Rendered text and approved linguistic attributes | Tag structure, attribute names, technical values, URLs, expressions, component names |
| Markdown | Prose, headings, link labels | Link destinations, code fences, inline code, directives |

Do not assume every string value is translatable. IDs, paths, enum values, SQL, CSS, commands, hashes, telemetry names, and machine prompts may need to remain exact.

## Translate with whole-file context

Read all related strings before translating a label. “Open,” “Close,” “Save,” or “Order” may be a verb, adjective, or noun. Determine grammatical gender, number, object, and screen context. Keep repeated product terms consistent without forcing one translation where the target language requires different inflection.

## Handle ICU and plurals natively

Preserve the source argument names and branch selectors, but write each branch as an independent native sentence. The target language may need different plural categories or sentence order. When changing categories is allowed by the platform, follow the target locale's CLDR plural rules; otherwise flag the structural limitation instead of faking grammar.

Example source:

```text
{count, plural, one {# file deleted} other {# files deleted}}
```

The translated branches need not mirror “number + noun + verb” if the target language naturally orders the information differently. `{count}`, `#`, `one`, and `other` remain formatter syntax.

## Preserve placeholders and markup

Treat these as protected unless the runtime specification says otherwise:

```text
{{user_name}}  ${total}  %{count}  {project}  %s  %1$d
https://example.com/path  <strong>...</strong>  `command --flag`
```

Placeholders may move to a grammatically natural position but must not be renamed, dropped, duplicated, or malformed. HTML tags may move with the phrase they mark when nesting remains valid. Never expose raw markup to the reader to avoid a difficult grammar problem.

## Validate mechanically and linguistically

Run the bundled guard on source and target files:

```bash
python3 scripts/translation_guard.py source.json target.json --format json
python3 scripts/translation_guard.py source.md target.md --format text
python3 scripts/translation_guard.py source.html target.html --format html
```

The guard checks structure, normalization, and protected tokens; it cannot judge meaning or naturalness. Follow it with the seven-pass quality gate in `SKILL.md`.

In HTML mode, the guard allows native translation of `alt`, `title`, `placeholder`, `aria-label`, and `aria-description`. It requires tag order, attribute names, technical attribute values, comments, declarations, scripts, and styles to remain intact. Placeholders, links, code, and other protected tokens inside translatable attributes must still match.

For formats not parsed by the guard, use the native parser or project test suite when available. Never rely only on visual inspection for machine-readable output.
