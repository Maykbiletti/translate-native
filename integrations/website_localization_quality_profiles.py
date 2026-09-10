#!/usr/bin/env python3
"""Versioned, provider-neutral quality profiles for every EU target locale."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any


SCHEMA = "blun.website-localization-quality-profile.v1"
CLDR_VERSION = "48"
CLDR_SUMMARY = "https://www.unicode.org/cldr/charts/48/summary/{language}.html"
MALTESE_ORTHOGRAPHY_SOURCE = (
    "https://kunsilltalmalti.gov.mt/mistoqssija-u-twegiba-51-76/"
)
REQUIRED_RED_TEAM_CHECKS = (
    "translationese",
    "wrong_neighbor_language",
    "mixed_language_varieties",
    "ascii_folding",
    "missing_diacritics_or_native_script",
    "wrong_inflection",
    "meaning_omission",
    "unnatural_cta",
    "marketing_calque",
)


@dataclass(frozen=True)
class LocaleQualityProfile:
    locale: str
    version: str
    native_review_focus: tuple[str, ...]
    fidelity_review_focus: tuple[str, ...]
    adversarial_focus: tuple[str, ...]
    required_red_team_checks: tuple[str, ...]
    source_refs: tuple[str, ...]

    def as_payload(self) -> dict[str, Any]:
        body = json.loads(_canonical_json({"schema": SCHEMA, **asdict(self)}))
        body["sha256"] = hashlib.sha256(_canonical_json(body)).hexdigest()
        return body


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _profile(
    locale: str,
    native: tuple[str, ...],
    fidelity: tuple[str, ...],
    adversarial: tuple[str, ...],
    *,
    sources: tuple[str, ...] = (),
) -> LocaleQualityProfile:
    language = locale.split("-", 1)[0]
    return LocaleQualityProfile(
        locale=locale,
        version=f"eu-{locale}-2026-09-1",
        native_review_focus=native,
        fidelity_review_focus=fidelity,
        adversarial_focus=adversarial,
        required_red_team_checks=REQUIRED_RED_TEAM_CHECKS,
        source_refs=(CLDR_SUMMARY.format(language=language), *sources),
    )


PROFILES: tuple[LocaleQualityProfile, ...] = (
    _profile(
        "bg-BG",
        ("Use idiomatic Bulgarian Cyrillic, natural clitic placement, verbal aspect, and postposed definite articles.",),
        ("Check aspect, tense, evidential meaning, negation, modality, number, gender, and agreement against the source.",),
        ("Reject transliteration, Russian or Macedonian leakage, missing article suffixes, stiff CTAs, marketing calques, omissions, and ASCII folding.",),
    ),
    _profile(
        "hr-HR",
        ("Use contemporary Croatian vocabulary, natural clitic order, case government, aspect, and idiomatic information structure.",),
        ("Check case roles, aspect, tense, modality, negation, quantities, terminology, and every source proposition.",),
        ("Reject Serbian or Bosnian variety mixing, ekavian leakage, lost diacritics, bad inflection, literal CTAs, marketing calques, and omissions.",),
    ),
    _profile(
        "cs-CZ",
        ("Use idiomatic Czech word order, aspect, case government, agreement, and native characters including č, ř, š, ě, ů, and ž.",),
        ("Check aspect, case roles, negation, modality, quantities, terminology, and complete propositional coverage.",),
        ("Reject Slovak leakage, ASCII-folded diacritics, false friends, malformed inflection, source-shaped CTAs, marketing calques, and omissions.",),
    ),
    _profile(
        "da-DK",
        ("Use contemporary Danish syntax, definiteness, compound formation, modal tone, and concise native web rhythm.",),
        ("Check definiteness, negation, modality, quantities, terminology, and the full source meaning.",),
        ("Reject Swedish or Norwegian leakage, mixed varieties, lost æ/ø/å, unnatural compounds, literal CTAs, marketing calques, and omissions.",),
    ),
    _profile(
        "nl-NL",
        ("Use Netherlands Dutch word order, separable verbs, natural compounds, register, and idiomatic web copy.",),
        ("Check verb particles, negation, modality, quantities, terminology, conditions, and complete source coverage.",),
        ("Reject Belgian-Dutch mixing, German or English calques, bad compounds, stiff CTAs, missing meaning, and inappropriate ASCII substitution.",),
    ),
    _profile(
        "en-IE",
        ("Use natural contemporary Irish English spelling, idiom, register, punctuation, and concise web-copy rhythm.",),
        ("Check negation, modality, quantities, terminology, conditions, and every source proposition without semantic inflation.",),
        ("Reject mixed US and Irish conventions, source-language syntax, translationese, unnatural CTAs, marketing calques, and omissions.",),
    ),
    _profile(
        "et-EE",
        ("Use idiomatic Estonian information structure, case government, quantity degrees, consonant gradation, compounds, and object case.",),
        ("Check case roles, total versus partial objects, negation, modality, quantities, terminology, and complete meaning.",),
        ("Reject Finnish or Russian leakage, ASCII-folded õ/ä/ö/ü, wrong quantity or inflection, literal CTAs, marketing calques, and omissions.",),
    ),
    _profile(
        "fi-FI",
        ("Use natural Finnish information structure, case government, agglutination, possessive suffixes, vowel harmony, consonant gradation, clitics, idiomatic compounds, and appropriate politeness.",),
        ("Check case roles, scope, negation, modality, quantities, terminology, conditions, and every proposition across the complete source.",),
        ("Reject Swedish, Estonian, English, or German leakage and calques; ASCII-folded ä/ö; broken inflection; unnatural CTAs; marketing calques; and omissions.",),
    ),
    _profile(
        "fr-FR",
        ("Use France French idiom, natural syntax, contractions, agreement, typography, register, and persuasive but credible web rhythm.",),
        ("Check gender and number agreement, negation, modality, quantities, terminology, conditions, and complete meaning.",),
        ("Reject Canadian or Belgian variety mixing, anglicisms, lost accents, literal CTAs, marketing calques, bad inflection, and omissions.",),
    ),
    _profile(
        "de-AT",
        ("Use Austrian Standard German vocabulary and conventions, idiomatic clause structure, compounds, case government, register, and natural web rhythm.",),
        ("Check case roles, negation, modality, quantities, terminology, conditions, and every source proposition.",),
        ("Reject Germany-only defaults where Austrian usage matters, Swiss mixing, ASCII-folded ä/ö/ü/ß, translationese, stiff CTAs, marketing calques, and omissions.",),
    ),
    _profile(
        "el-GR",
        ("Use monotonic Greek with native Greek script, correct accents, aspect, clitic placement, case, gender, agreement, and natural information structure.",),
        ("Check aspect, case roles, negation, modality, quantities, terminology, conditions, and complete source coverage.",),
        ("Reject Greeklish, Ancient or Cypriot variety mixing, missing tonos, bad inflection, literal CTAs, marketing calques, and omissions.",),
    ),
    _profile(
        "hu-HU",
        ("Use idiomatic Hungarian focus order, vowel harmony, case suffixes, definite or indefinite conjugation, compounds, and natural politeness.",),
        ("Check focus and scope, conjugation, negation, modality, quantities, terminology, conditions, and complete meaning.",),
        ("Reject neighboring-language leakage, ASCII-folded ő/ű, suffix disharmony, bad inflection, source-shaped CTAs, marketing calques, and omissions.",),
    ),
    _profile(
        "ga-IE",
        ("Use contemporary standard Irish with natural VSO structure, initial mutations, broad or slender consonant spelling, case forms, copula versus bí, and appropriate register.",),
        ("Check mutations, possession, genitive relations, negation, modality, quantities, terminology, and every source proposition.",),
        ("Reject English word order and calques, Scottish Gaelic leakage, missing fadas, wrong mutations, unnatural CTAs, marketing calques, and omissions.",),
    ),
    _profile(
        "it-IT",
        ("Use contemporary Italian idiom, natural clitic placement, article and preposition contraction, agreement, register, and web-copy rhythm.",),
        ("Check clitic reference, negation, modality, quantities, terminology, conditions, and complete source meaning.",),
        ("Reject regional or Swiss-Italian mixing, anglicised syntax, lost accents, bad agreement, literal CTAs, marketing calques, and omissions.",),
    ),
    _profile(
        "lv-LV",
        ("Use idiomatic Latvian case government, declension, agreement, verb prefixes, information structure, and native diacritics.",),
        ("Check case roles, aspectual prefixes, negation, modality, quantities, terminology, conditions, and complete meaning.",),
        ("Reject Lithuanian or Russian leakage, ASCII-folded ā/č/ē/ģ/ī/ķ/ļ/ņ/š/ū/ž, bad inflection, literal CTAs, marketing calques, and omissions.",),
    ),
    _profile(
        "lt-LT",
        ("Use idiomatic Lithuanian case government, declension, agreement, participles, aspect, information structure, and native diacritics.",),
        ("Check case roles, aspect, negation, modality, quantities, terminology, conditions, and complete source coverage.",),
        ("Reject Latvian, Polish, or Russian leakage, ASCII-folded ą/č/ę/ė/į/š/ų/ū/ž, bad inflection, literal CTAs, marketing calques, and omissions.",),
    ),
    _profile(
        "mt-MT",
        ("Use contemporary Maltese orthography with ċ, ġ, għ, ħ, and ż; natural Semitic and Romance morphology; fused articles and prepositions; idiomatic syntax; register; and rhythm.",),
        ("Check articles, prepositions, agreement, verb morphology, negation, modality, quantities, terminology, conditions, and every source proposition.",),
        ("Reject English or Italian calques and word order, neighboring-language leakage, ASCII-folded Maltese letters, false plurals or inflection, unnatural CTAs, marketing calques, and omissions.",),
        sources=(MALTESE_ORTHOGRAPHY_SOURCE,),
    ),
    _profile(
        "pl-PL",
        ("Use idiomatic Polish aspect, case government, gender and virility agreement, natural word order, register, and native diacritics.",),
        ("Check aspect, case roles, negation, modality, quantities, terminology, conditions, and complete meaning.",),
        ("Reject Czech, Slovak, or Russian leakage, ASCII-folded ą/ć/ę/ł/ń/ó/ś/ź/ż, bad inflection, literal CTAs, marketing calques, and omissions.",),
    ),
    _profile(
        "pt-PT",
        ("Use European Portuguese vocabulary, clitic placement, infinitive choices, agreement, register, punctuation, and natural web rhythm.",),
        ("Check clitic reference, negation, modality, quantities, terminology, conditions, and every source proposition.",),
        ("Reject Brazilian-Portuguese mixing, Spanish or English calques, lost diacritics, bad inflection, unnatural CTAs, marketing calques, and omissions.",),
    ),
    _profile(
        "ro-RO",
        ("Use contemporary Romanian idiom, enclitic definite articles, case marking, agreement, clitic placement, register, and native diacritics.",),
        ("Check article and clitic reference, negation, modality, quantities, terminology, conditions, and complete meaning.",),
        ("Reject Moldovan or Italian mixing, ASCII-folded ă/â/î/ș/ț, cedilla substitutions, bad inflection, literal CTAs, marketing calques, and omissions.",),
    ),
    _profile(
        "sk-SK",
        ("Use idiomatic Slovak aspect, case government, agreement, rhythmic law where relevant, natural word order, and native diacritics.",),
        ("Check aspect, case roles, negation, modality, quantities, terminology, conditions, and complete source coverage.",),
        ("Reject Czech leakage, ASCII-folded ľ/ĺ/ŕ/ť/ď/ň/ô/ä, bad inflection, source-shaped CTAs, marketing calques, and omissions.",),
    ),
    _profile(
        "sl-SI",
        ("Use idiomatic Slovenian dual forms, case government, agreement, aspect, clitic order, natural information structure, and native diacritics.",),
        ("Check dual and plural number, case roles, negation, modality, quantities, terminology, conditions, and complete meaning.",),
        ("Reject Croatian or Serbian leakage, ASCII-folded č/š/ž, lost dual forms, bad inflection, literal CTAs, marketing calques, and omissions.",),
    ),
    _profile(
        "es-ES",
        ("Use Spain Spanish idiom, natural clitic use, mood and aspect, agreement, register, punctuation, and credible web-copy rhythm.",),
        ("Check mood, aspect, pronoun reference, negation, modality, quantities, terminology, conditions, and complete meaning.",),
        ("Reject Latin-American variety mixing, anglicisms, lost accents or ñ, bad inflection, literal CTAs, marketing calques, and omissions.",),
    ),
    _profile(
        "sv-SE",
        ("Use Sweden Swedish word order, definiteness, compound formation, modal tone, punctuation, register, and concise native web rhythm.",),
        ("Check definiteness, negation, modality, quantities, terminology, conditions, and every source proposition.",),
        ("Reject Finnish-Swedish, Danish, or Norwegian mixing, ASCII-folded å/ä/ö, split compounds, literal CTAs, marketing calques, and omissions.",),
    ),
)

_BY_LOCALE = {profile.locale: profile for profile in PROFILES}
if len(_BY_LOCALE) != len(PROFILES):
    raise RuntimeError("duplicate website-localization quality profile")


def quality_profile_for(locale: str) -> dict[str, Any]:
    try:
        profile = _BY_LOCALE[locale]
    except (KeyError, TypeError):
        raise ValueError("unsupported quality-profile locale") from None
    return profile.as_payload()
