# Native Translation Standard

## The core distinction

A correct translation preserves the source meaning. A native translation also follows the target language's own habits of thought and expression. Both are required.

Do not optimize for visible word correspondence. Optimize for semantic equivalence plus native acceptability. A sentence may need to be split, merged, reordered, made implicit, or made explicit where the target grammar conventionally requires it, provided no source claim changes.

## Choose the operating mode

| Mode | Primary goal | Permitted adaptation |
| --- | --- | --- |
| Faithful native translation | Preserve meaning in fully native language | Syntax, idiom, information order, punctuation |
| Localization | Make content work in a locale or product | Formats, units, conventions, locally expected labels |
| Transcreation | Preserve effect in persuasive or creative work | Imagery, wordplay, slogans, cultural framing—with claims intact |
| Proofreading | Repair target-language text | Grammar, vocabulary, nativeness, consistency |

Use faithful native translation by default. Do not silently turn translation into copywriting.

## Specify language beyond its name

Represent the target as a bundle:

- language and BCP 47 tag where known;
- region or speech community;
- script and orthography;
- formal, informal, honorific, technical, literary, or conversational register;
- audience age, expertise, and relationship to the writer;
- medium and space constraints.

Language labels can hide materially different choices. “Chinese” requires a script and often a region; “Arabic” can mean Modern Standard Arabic or a spoken variety; “Spanish,” “Portuguese,” “French,” “English,” “Catalan,” “Serbian,” and many others have regional standards. Prefer the user's explicit identity and terminology over assumptions.

## Preserve semantic force

Check especially:

- scope of negation and quantifiers;
- can/may/must/should and their degree of obligation;
- completed, ongoing, habitual, and hypothetical actions;
- evidentiality, confidence, hearsay, and uncertainty;
- inclusive/exclusive pronouns and levels of respect;
- gender, number, animacy, classifiers, and noun classes where relevant;
- relationships between headings, labels, buttons, and surrounding instructions.

Naturalness never licenses stronger claims, extra benefits, removed caveats, or invented politeness.

## Respect the language community

- Use the script and orthography used by the requested community, including diacritics, combining marks, contextual forms, native punctuation, spacing, and directionality.
- Do not treat Latin transliteration as the default for a non-Latin script.
- Avoid exoticizing minority and Indigenous languages or replacing community terminology with a majority-language label.
- When multiple standards exist, name the selected standard if that choice matters.
- Preserve names according to the person's or organization's established form. Transliterate only according to the target convention or explicit request.

## Source hierarchy for verification

Use evidence in roughly this order, adjusting for the language and domain:

1. Native-authored or professionally native-edited material in the same domain and locale.
2. Language academies, community authorities, government terminology banks, universities, and respected dictionaries or style guides.
3. Unicode CLDR and BCP 47 resources for locale identifiers, formats, exemplar characters, plurals, and writing-system conventions.
4. Curated monolingual corpora such as the Leipzig Corpora Collection for collocations and contemporary usage.
5. Curated evaluation sets such as FLORES+ for cross-language comparison.
6. Parallel corpora such as OPUS as supporting evidence only.
7. Uncurated web text or anonymous dataset entries only as weak corroboration.

A large dataset is not automatically authoritative. It may contain machine translations, subtitles, duplicated text, wrong language tags, domain bias, outdated orthography, or misaligned sentences. Check its dataset card, provenance, license, curation method, date, locale, and intended use.

Useful starting points:

- FLORES+: <https://huggingface.co/datasets/openlanguagedata/flores_plus>
- OPUS: <https://opus.nlpl.eu/>
- Leipzig Corpora Collection: <https://cls.corpora.uni-leipzig.de/>
- Unicode CLDR: <https://cldr.unicode.org/>

Do not download large corpora for a routine translation. Consult only when the wording or language variety needs verification.

## Detect translationese

Revise when any symptom appears:

- target sentences mirror source length and clause order without linguistic reason;
- dictionary synonyms replace established collocations;
- pronouns are repeated or omitted according to the source rather than target norms;
- politeness, articles, tense, classifiers, particles, or discourse markers feel imported;
- headlines and buttons use sentence grammar instead of the target medium's conventions;
- an idiom is understandable but no native writer would choose it;
- every sentence is grammatical yet the paragraph does not flow natively.

Use a back-translation only as a diagnostic for missing meaning, never as the final naturalness test.

## Handle uncertainty honestly

For low-resource languages, fluent-looking output can still be wrong. If evidence is thin, separate what is known from what is inferred, avoid mixing neighboring varieties, and request community or professional review for consequential use. Do not call a translation native merely because it is grammatical.
