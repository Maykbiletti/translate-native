#!/usr/bin/env python3
"""Versioned, provider-neutral quality profiles for every EU target locale."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any


SCHEMA = "blun.website-localization-quality-profile.v1"
COMMERCIAL_SCHEMA = "translate-native.commercial-locale-quality-profile.v2"
COMMERCIAL_RENDERING_SCHEMA = "translate-native.commercial-rendering-reference.v1"
CLDR_VERSION = "48"
CLDR_SUMMARY = "https://www.unicode.org/cldr/charts/48/summary/{language}.html"
CLDR_NUMBERS = (
    "https://github.com/unicode-org/cldr-json/blob/48.0.0/"
    "cldr-json/cldr-numbers-full/main/{locale}/numbers.json"
)
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
COMMERCIAL_REVIEW_CHECKS = (
    "amount_currency",
    "discount_basis",
    "qualifiers",
    "tax_status",
    "billing_interval",
    "commitment",
    "renewal",
    "cancellation",
    "conditions",
    "offer_assignment",
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


@dataclass(frozen=True)
class CommercialRenderingReference:
    locale: str
    cldr_locale: str
    numbering_system: str
    native_numbering_system: str
    minimum_grouping_digits: int
    decimal_symbol: str
    grouping_symbol: str
    decimal_pattern: str
    percent_pattern: str
    currency_pattern: str
    currency_alpha_pattern: str
    currency_append_iso_pattern: str
    approximately_pattern: str
    at_least_pattern: str
    at_most_pattern: str
    range_pattern: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema": COMMERCIAL_RENDERING_SCHEMA,
            "locale": self.locale,
            "source": {
                "authority": "Unicode CLDR",
                "version": CLDR_VERSION,
                "locale": self.cldr_locale,
                "url": CLDR_NUMBERS.format(locale=self.cldr_locale),
            },
            "numbering_system": self.numbering_system,
            "native_numbering_system": self.native_numbering_system,
            "minimum_grouping_digits": self.minimum_grouping_digits,
            "symbols": {
                "decimal": self.decimal_symbol,
                "group": self.grouping_symbol,
            },
            "patterns": {
                "decimal": self.decimal_pattern,
                "percent": self.percent_pattern,
                "currency": self.currency_pattern,
                "currency_alpha_next_to_number": self.currency_alpha_pattern,
                "currency_append_iso": self.currency_append_iso_pattern,
                "approximately": self.approximately_pattern,
                "at_least": self.at_least_pattern,
                "at_most": self.at_most_pattern,
                "range": self.range_pattern,
            },
            "application": {
                "purpose": "target-locale-rendering-guidance",
                "semantic_proof": False,
                "allow_equivalent_number_words": True,
                "allow_equivalent_written_percentages": True,
                "allow_equivalent_digit_forms": True,
                "preserve_exact_value_and_currency_identity": True,
                "rounding_allowed": False,
                "currency_conversion_allowed": False,
                "ambiguous_values": "targeted-review",
                "unresolved_route": (
                    "independent-model-or-qualified-native-domain-review"
                ),
            },
        }


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

_NBSP = "\N{NO-BREAK SPACE}"
_NNBSP = "\N{NARROW NO-BREAK SPACE}"


def _rendering(
    locale: str,
    cldr_locale: str,
    *,
    minimum_grouping_digits: int = 1,
    decimal_symbol: str = ",",
    grouping_symbol: str = _NBSP,
    percent_pattern: str = f"#,##0{_NBSP}%",
    currency_pattern: str = f"#,##0.00{_NBSP}¤",
    currency_alpha_pattern: str | None = None,
    currency_append_iso_pattern: str = f"{{0}}{_NBSP}¤¤",
    approximately_pattern: str = "~{0}",
    at_least_pattern: str = "≥{0}",
    at_most_pattern: str = "≤{0}",
    range_pattern: str = "{0}–{1}",
) -> CommercialRenderingReference:
    return CommercialRenderingReference(
        locale=locale,
        cldr_locale=cldr_locale,
        numbering_system="latn",
        native_numbering_system="latn",
        minimum_grouping_digits=minimum_grouping_digits,
        decimal_symbol=decimal_symbol,
        grouping_symbol=grouping_symbol,
        decimal_pattern="#,##0.###",
        percent_pattern=percent_pattern,
        currency_pattern=currency_pattern,
        currency_alpha_pattern=currency_alpha_pattern or currency_pattern,
        currency_append_iso_pattern=currency_append_iso_pattern,
        approximately_pattern=approximately_pattern,
        at_least_pattern=at_least_pattern,
        at_most_pattern=at_most_pattern,
        range_pattern=range_pattern,
    )


# CLDR 48 parent data is used unless the chosen BCP-47 profile has an explicit
# regional override. These are rendering references, never semantic validators.
COMMERCIAL_RENDERING_REFERENCES: tuple[CommercialRenderingReference, ...] = (
    _rendering(
        "bg-BG", "bg", minimum_grouping_digits=2,
        percent_pattern="#,##0%", at_least_pattern="≥ {0}",
        at_most_pattern="≤ {0}", range_pattern="{0} – {1}",
    ),
    _rendering(
        "hr-HR", "hr", grouping_symbol=".", at_least_pattern="{0}+",
        range_pattern="{0} – {1}",
    ),
    _rendering(
        "cs-CZ", "cs", currency_append_iso_pattern="{0} ¤¤",
    ),
    _rendering(
        "da-DK", "da", grouping_symbol=".", at_least_pattern="{0}+",
        range_pattern="{0}-{1}",
    ),
    _rendering(
        "nl-NL", "nl", grouping_symbol=".", percent_pattern="#,##0%",
        currency_pattern=f"¤{_NBSP}#,##0.00;¤{_NBSP}-#,##0.00",
        at_least_pattern="{0}+", range_pattern="{0}-{1}",
    ),
    _rendering(
        "en-IE", "en-IE", decimal_symbol=".", grouping_symbol=",",
        percent_pattern="#,##0%", currency_pattern="¤#,##0.00",
        currency_alpha_pattern=f"¤{_NBSP}#,##0.00",
        at_least_pattern="{0}+",
    ),
    _rendering(
        "et-EE", "et", minimum_grouping_digits=2,
        percent_pattern="#,##0%", approximately_pattern="~ {0}",
        at_most_pattern="≤ {0}", range_pattern="{0}‒{1}",
    ),
    _rendering("fi-FI", "fi", at_least_pattern="vähintään {0}"),
    _rendering(
        "fr-FR", "fr", grouping_symbol=_NNBSP,
        approximately_pattern="≈{0}",
    ),
    _rendering(
        "de-AT", "de-AT", currency_pattern=f"¤{_NBSP}#,##0.00",
        approximately_pattern="≈{0}", at_least_pattern="{0}+",
    ),
    _rendering(
        "el-GR", "el", grouping_symbol=".", percent_pattern="#,##0%",
        at_least_pattern="{0}+",
    ),
    _rendering(
        "hu-HU", "hu", minimum_grouping_digits=2,
        percent_pattern="#,##0%", at_least_pattern="{0}+",
    ),
    _rendering(
        "ga-IE", "ga", decimal_symbol=".", grouping_symbol=",",
        percent_pattern="#,##0%", currency_pattern="¤#,##0.00",
        currency_alpha_pattern=f"¤{_NBSP}#,##0.00",
        at_least_pattern="{0}+",
    ),
    _rendering(
        "it-IT", "it", minimum_grouping_digits=2, grouping_symbol=".",
        percent_pattern="#,##0%", range_pattern="{0}-{1}",
    ),
    _rendering(
        "lv-LV", "lv", minimum_grouping_digits=2,
        percent_pattern="#,##0%",
    ),
    _rendering("lt-LT", "lt"),
    _rendering(
        "mt-MT", "mt", decimal_symbol=".", grouping_symbol=",",
        percent_pattern="#,##0%", currency_pattern="¤#,##0.00",
        currency_alpha_pattern=f"¤{_NBSP}#,##0.00",
    ),
    _rendering(
        "pl-PL", "pl", minimum_grouping_digits=2,
        percent_pattern="#,##0%",
    ),
    _rendering(
        "pt-PT", "pt-PT", minimum_grouping_digits=2,
        percent_pattern="#,##0%", at_least_pattern="+{0}",
        range_pattern="{0} - {1}",
    ),
    _rendering(
        "ro-RO", "ro", grouping_symbol=".",
        range_pattern="{0} - {1}",
    ),
    _rendering(
        "sk-SK", "sk", at_least_pattern="{0}+",
        range_pattern="{0} – {1}",
    ),
    _rendering(
        "sl-SI", "sl", minimum_grouping_digits=2, grouping_symbol=".",
        approximately_pattern="~ {0}", at_least_pattern="≥ {0}",
        at_most_pattern="≤ {0}",
    ),
    _rendering(
        "es-ES", "es", minimum_grouping_digits=2, grouping_symbol=".",
        at_least_pattern="Más de {0}", range_pattern="{0}-{1}",
    ),
    _rendering("sv-SE", "sv", at_least_pattern="⩾{0}"),
)

_BY_LOCALE = {profile.locale: profile for profile in PROFILES}
if len(_BY_LOCALE) != len(PROFILES):
    raise RuntimeError("duplicate website-localization quality profile")
_COMMERCIAL_RENDERING_BY_LOCALE = {
    reference.locale: reference for reference in COMMERCIAL_RENDERING_REFERENCES
}
if set(_COMMERCIAL_RENDERING_BY_LOCALE) != set(_BY_LOCALE):
    raise RuntimeError("commercial rendering and quality-profile registries differ")


def quality_profile_for(locale: str) -> dict[str, Any]:
    try:
        profile = _BY_LOCALE[locale]
    except (KeyError, TypeError):
        raise ValueError("unsupported quality-profile locale") from None
    return profile.as_payload()


def commercial_quality_profile_for(
    locale: str,
    commercial_profile: str,
) -> dict[str, Any]:
    """Bind commercial evaluation to one exact locale quality generation."""

    try:
        profile = _BY_LOCALE[locale]
    except (KeyError, TypeError):
        raise ValueError("unsupported commercial quality-profile locale") from None
    if (
        not isinstance(commercial_profile, str)
        or not commercial_profile
        or commercial_profile != commercial_profile.strip()
        or len(commercial_profile) > 256
    ):
        raise ValueError("commercial profile is invalid")
    quality = profile.as_payload()
    rendering = _COMMERCIAL_RENDERING_BY_LOCALE[locale].as_payload()
    body = {
        "schema": COMMERCIAL_SCHEMA,
        "locale": locale,
        "version": f"commercial-eu-{locale}-2026-09-2",
        "commercial_profile": commercial_profile,
        "quality_profile_version": quality["version"],
        "quality_profile_sha256": quality["sha256"],
        "rendering_reference": rendering,
        "creation_focus": [
            *quality["native_review_focus"],
            (
                "Write prices, offers, billing intervals, commitments, renewal, "
                "cancellation and conditions as natural native commercial copy "
                "without changing any proposition or its offer assignment."
            ),
            (
                "Apply rendering_reference for target-locale number, percent, "
                "currency, spacing, approximation, limit and range conventions; "
                "do not round values or convert currencies."
            ),
        ],
        "native_review_focus": [
            *quality["native_review_focus"],
            (
                "Reject source-shaped price labels, interval phrases, CTAs and "
                "terms; allow locale-appropriate number, currency, spacing and "
                "punctuation conventions when the commercial meaning is exact."
            ),
            (
                "Judge numeric and currency typography against rendering_reference "
                "while accepting natural equivalent number words, written "
                "percentages and digit forms."
            ),
        ],
        "fidelity_review_focus": [
            *quality["fidelity_review_focus"],
            (
                "Check every commercial proposition and footnote against its own "
                "offer, including values, limits, timing, tax status and conditions."
            ),
            (
                "Use rendering_reference only to interpret locale formatting, "
                "never as deterministic proof of value equality; route ambiguous "
                "values to targeted review."
            ),
        ],
        "adversarial_focus": [
            *quality["adversarial_focus"],
            (
                "Reject swapped offer terms, hidden qualifiers, changed billing "
                "or commitment periods, strengthened discounts, and invented tax, "
                "renewal or cancellation claims."
            ),
        ],
        "required_commercial_checks": list(COMMERCIAL_REVIEW_CHECKS),
        "source_refs": quality["source_refs"],
    }
    detached = json.loads(_canonical_json(body))
    detached["sha256"] = hashlib.sha256(_canonical_json(detached)).hexdigest()
    return detached
