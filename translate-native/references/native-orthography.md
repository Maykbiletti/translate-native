# Native Orthography Gate

## Release rule

Write every target in its native orthography. Correct spelling, language-specific letters, diacritics, alphabet, script, punctuation, casing, spacing, and Unicode normalization are mandatory parts of translation quality, not optional typography.

Do not rely on a separate skill to perform this review. Do not release a translation while a confirmed orthographic defect remains.

## Mandatory review

1. Identify the exact target language, locale, script, and regional standard.
2. Review every natural-language segment, including headings, buttons, labels, metadata, accessibility text, and mixed-language passages.
3. Replace ASCII transliterations only when they stand in for native spelling: `schön`, not `schoen`; `förstår`, not `forstar`; `español`, not `espanol`; `čeština`, not `cestina`.
4. Use the native writing system by default. Do not romanize Greek, Cyrillic, Arabic, Hebrew, Indic, Southeast Asian, or East Asian text unless requested.
5. Apply locale-specific punctuation and typography, such as Spanish `¿…?` and `¡…!`, French spacing conventions, or Chinese and Japanese punctuation.
6. Preserve the canonical spelling of names, brands, quotations, and established technical terms. Verify uncertainty instead of inventing a mark.
7. Normalize newly written UTF-8 content to Unicode NFC. Preserve a different normalization only when a technical format explicitly requires it.
8. Re-run the full language and fidelity review after any orthographic correction that could change meaning.

## Coverage map

| Language or writing system | Preserve |
| --- | --- |
| German | `ä ö ü Ä Ö Ü ß`; use Swiss `ss` where required |
| Swedish | `å ä ö Å Ä Ö` as separate letters |
| Danish and Norwegian | `æ ø å Æ Ø Å` |
| Icelandic and Faroese | accents and letters such as `ð þ æ ö ø` |
| Spanish | `á é í ó ú ü ñ ¿ ¡` |
| French | accents, diaeresis, cedilla, and conventional ligatures |
| Portuguese | acute and circumflex accents, tildes, grave accents, and cedilla |
| Catalan, Galician, and Basque | native accents, diaeresis, cedilla, middle dot, and `ñ` where applicable |
| Czech and Slovak | every caron, acute, ring, circumflex, diaeresis, and language-specific consonant |
| Polish, Hungarian, Romanian | complete native diacritics; Romanian comma-below `ș ț`, not cedilla variants |
| Turkish and Azerbaijani | dotted and undotted `i`, `ç ğ ö ş ü`, and Azerbaijani `ə` |
| South Slavic Latin scripts | `č ć đ š ž` and the exact requested regional standard |
| Baltic languages | macrons, carons, dots, cedillas, and ogoneks |
| Vietnamese | complete tone and vowel marks, including stacked marks |
| Greek | Greek alphabet with correct tonos and dialytika; no Greeklish by default |
| Cyrillic languages | the exact language alphabet, including letters absent from Russian |
| Arabic-script languages | the correct language-specific letters and punctuation; optional vowel marks only when conventional |
| Hebrew | Hebrew script; preserve or add niqqud only when the task requires it |
| Indic scripts | native letters, vowel signs, conjuncts, nukta, virama, and punctuation |
| Thai, Lao, Khmer, and Myanmar | tone marks, vowel placement, combining marks, and native spacing conventions |
| Chinese | requested Simplified or Traditional characters and locale-appropriate punctuation |
| Japanese | appropriate kanji, kana, prolonged-sound and iteration marks, spacing, and punctuation |
| Korean | Hangul and correct Korean spacing; preserve hanja only when intended |
| Armenian, Georgian, Ethiopic, and other scripts | native alphabet, canonical spelling, and punctuation |

This map is illustrative, never exhaustive. Apply the same native-orthography rule to every human language and writing system.

## Technical exceptions

Keep exact ASCII or source spelling when modification could break behavior or fidelity:

- code identifiers, commands, environment variables, API fields, JSON keys, database columns, slugs, URLs, email addresses, paths, hashes, tokens, regular expressions, and protocol values;
- exact quotations, imported data, trademarks, and personal names whose canonical spelling is known;
- systems with a documented encoding limitation;
- explicit requests for transliteration, ASCII folding, search keys, or URL-safe text.

Technical exceptions never justify ASCII-only prose around protected values.

## Limits of automated checks

The bundled translation guard verifies UTF-8, Unicode NFC, protected tokens, and structure. The bundled diacritics linter flags frequent ASCII substitutions in many Latin-script languages while ignoring common technical spans. Neither can prove that a word contains the correct diacritic, that the requested script was chosen, or that spelling is native. Use deterministic checks as evidence, then complete the language-aware review manually or with a qualified native reviewer.

## Verification hierarchy

When usage, spelling, script, or locale conventions are uncertain, verify in this order:

1. the requested language community's academy, language council, government style guide, or other recognized normative body;
2. the [Unicode Standard](https://unicode.org/reports/tr15/) for normalization and character behavior;
3. [Unicode CLDR](https://cldr.unicode.org/) for locale identifiers, exemplar characters, punctuation sets, plural rules, number and date conventions, and script variants;
4. the [W3C Language Enablement Index](https://www.w3.org/International/typography/gap-analysis/language-matrix.html) for writing-system layout and typography;
5. the [IANA Language Subtag Registry](https://www.iana.org/assignments/language-subtag-registry) for language, region, and script tags;
6. contemporary native-edited dictionaries and monolingual corpora for usage and collocations.

Examples of authoritative language sources include Sweden's [Institutet för språk och folkminnen](https://www.isof.se/svenska-spraket/frageladan), the [Real Academia Española](https://www.rae.es/), the Czech Academy's [Czech Language Institute](https://ujc.cas.cz/), the [Institut d'Estudis Catalans](https://www.iec.cat/), and [Euskaltzaindia](https://www.euskaltzaindia.eus/).

Do not treat a translation engine, parallel corpus, search-result count, ASCII spell-checker, or another model's confidence as a normative source. Use them only to locate questions that still require native evidence.
