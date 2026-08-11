#!/usr/bin/env python3
"""Flag common ASCII substitutions and non-NFC text in prose files."""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from pathlib import Path


RULES: dict[str, tuple[tuple[str, str], ...]] = {
    "de": (
        (r"\bfuer\b", "für"),
        (r"\bueber", "über…"),
        (r"\bzurueck", "zurück…"),
        (r"\bmuess", "müss…"),
        (r"\bkoenn", "könn…"),
        (r"\bmoech", "möch…"),
        (r"\bschoen", "schön…"),
        (r"\bgrues", "grüß…"),
        (r"\bkoeln\b", "Köln"),
        (r"\bgroess", "größ…"),
        (r"\baender", "änder…"),
        (r"\bpruef", "prüf…"),
        (r"\bgepruef", "geprüf…"),
        (r"\buebersetz", "übersetz…"),
        (r"\boeffn", "öffn…"),
        (r"\bloesch", "lösch…"),
        (r"\bwaehr", "währ…"),
        (r"\bwaer(?:e|en|st|t)\b", "wär…"),
        (r"\bhaett", "hätt…"),
        (r"\bwuerd", "würd…"),
        (r"\bduerf", "dürf…"),
        (r"\berklaer", "erklär…"),
        (r"\bgeklaer", "geklär…"),
        (r"\bnaechst", "nächst…"),
        (r"\bspaet", "spät…"),
        (r"\bfrueh", "früh…"),
        (r"\bwuensch", "wünsch…"),
        (r"\bgueltig", "gültig…"),
        (r"\bzuverlaess", "zuverläss…"),
        (r"\bhaeufig", "häufig…"),
        (r"\bzusaetz", "zusätz…"),
        (r"\bvollstaendig", "vollständig…"),
        (r"\bpersoenlich", "persönlich…"),
        (r"\bnatuerlich", "natürlich…"),
        (r"\bmoeglich", "möglich…"),
        (r"\bnoetig", "nötig…"),
        (r"\baehnlich", "ähnlich…"),
        (r"\bgehoer", "gehör…"),
        (r"\benthaelt", "enthält"),
        (r"\bwaehl", "wähl…"),
        (r"\bveroeffent", "veröffent…"),
        (r"\bunterstuetz", "unterstütz…"),
        (r"\bbestaet", "bestät…"),
        (r"\bergaenz", "ergänz…"),
    ),
    "sv": (
        (r"\bGoteborg\b", "Göteborg"),
        (r"\bMalmo\b", "Malmö"),
        (r"\bOresund\b", "Öresund"),
        (r"\bsmorgasbord\b", "smörgåsbord"),
        (r"\bAngstrom\b", "Ångström"),
        (r"\bforstar\b", "förstår"),
        (r"\bbehover\b", "behöver"),
        (r"\bmojlig", "möjlig…"),
        (r"\bborja", "börja…"),
        (r"\bfraga", "fråga…"),
        (r"\bsjalv", "själv…"),
        (r"\bhar ar\b", "här är"),
        (r"\bdet ar\b", "det är"),
        (r"\bjag ar\b", "jag är"),
        (r"\bvi ar\b", "vi är"),
        (r"\bdu ar\b", "du är"),
        (r"\bar det\b", "är det"),
        (r"\bfor att\b", "för att"),
        (r"\bfor dig\b", "för dig"),
        (r"\bfor er\b", "för er"),
        (r"\bpa en\b", "på en"),
        (r"\bpa ett\b", "på ett"),
        (r"\bpa den\b", "på den"),
        (r"\bpa det\b", "på det"),
        (r"\bsprak", "språk…"),
        (r"\boppn", "öppn…"),
    ),
    "es": (
        (r"\bespanol\b", "español"),
        (r"\bsenor", "señor…"),
        (r"\binformacion\b", "información"),
        (r"\btambien\b", "también"),
        (r"\bpagina\b", "página"),
        (r"\bnumero\b", "número"),
        (r"\badios\b", "adiós"),
        (r"\bQue\?", "¿Qué?"),
    ),
    "fr": (
        (r"\bfrancais\b", "français"),
        (r"\becole\b", "école"),
        (r"\bcreme brulee\b", "crème brûlée"),
        (r"\btres\b", "très"),
        (r"\bdeja\b", "déjà"),
        (r"\betre\b", "être"),
    ),
    "pt": (
        (r"\bSao Paulo\b", "São Paulo"),
        (r"\bcoracao\b", "coração"),
        (r"\bportugues\b", "português"),
        (r"\bvoce\b", "você"),
        (r"\bnao\b", "não"),
    ),
    "it": (
        (r"\bperche\b", "perché"),
        (r"\bcitta\b", "città"),
        (r"\bpiu\b", "più"),
    ),
    "da": (
        (r"\bKobenhavn\b", "København"),
        (r"\bvaer", "vær…"),
        (r"\bsprogforstaelse\b", "sprogforståelse"),
    ),
    "no": (
        (r"\bvaer", "vær…"),
        (r"\bforsta", "forstå…"),
        (r"\bsporsmal\b", "spørsmål"),
    ),
    "is": (
        (r"\bReykjavik\b", "Reykjavík"),
        (r"\bThingvellir\b", "Þingvellir"),
        (r"\bislenska\b", "íslenska"),
    ),
    "cs": (
        (r"\bcestina\b", "čeština"),
        (r"\bcesk", "česk…"),
        (r"\bprilis\b", "příliš"),
        (r"\bDvorak\b", "Dvořák"),
        (r"\bdekuji\b", "děkuji"),
        (r"\bmesto\b", "město"),
    ),
    "sk": (
        (r"\bslovencina\b", "slovenčina"),
        (r"\bdakujem\b", "ďakujem"),
    ),
    "pl": (
        (r"\bLodz\b", "Łódź"),
        (r"\bjezyk\b", "język"),
        (r"\bdziekuje\b", "dziękuję"),
        (r"\bzolty\b", "żółty"),
    ),
    "hu": (
        (r"\bMagyarorszag\b", "Magyarország"),
        (r"\bkoszonom\b", "köszönöm"),
        (r"\borom\b", "öröm"),
    ),
    "ro": (
        (r"\bromana\b", "română"),
        (r"\bBucuresti\b", "București"),
        (r"\bmultumesc\b", "mulțumesc"),
        (r"\btara\b", "țară"),
    ),
    "tr": (
        (r"\bIstanbul\b", "İstanbul"),
        (r"\bTurkce\b", "Türkçe"),
        (r"\btesekkur", "teşekkür…"),
        (r"\bgorusuruz\b", "görüşürüz"),
    ),
    "hr": (
        (r"\bDorde\b", "Đorđe"),
        (r"\bvec\b", "već"),
        (r"\bcovjek\b", "čovjek"),
    ),
    "bs": (
        (r"\bDorde\b", "Đorđe"),
        (r"\bvec\b", "već"),
        (r"\bcovjek\b", "čovjek"),
    ),
    "sr": (
        (r"\bDorde\b", "Đorđe"),
        (r"\bvec\b", "već"),
        (r"\bcovek\b", "čovek"),
    ),
    "sl": (
        (r"\bslovenscina\b", "slovenščina"),
        (r"\bzivjo\b", "živjo"),
    ),
    "nl": (
        (r"\bgeinteresseerd\b", "geïnteresseerd"),
    ),
    "fi": (
        (r"\bhyvaa\b", "hyvää"),
        (r"\bpaiva\b", "päivä"),
    ),
    "et": (
        (r"\bTonu\b", "Tõnu"),
        (r"\bvoimalik\b", "võimalik"),
    ),
    "lv": (
        (r"\bRiga\b", "Rīga"),
        (r"\blatviesu\b", "latviešu"),
    ),
    "lt": (
        (r"\blietuviu\b", "lietuvių"),
        (r"\baciu\b", "ačiū"),
    ),
    "vi": (
        (r"\bTieng Viet\b", "Tiếng Việt"),
        (r"\bcam on\b", "cảm ơn"),
        (r"\bViet Nam\b", "Việt Nam"),
    ),
    "ca": (
        (r"\bcatala\b", "català"),
        (r"\bcollegi\b", "col·legi"),
    ),
    "ga": (
        (r"\bSlainte\b", "Sláinte"),
        (r"\bfailte\b", "fáilte"),
    ),
    "mt": (
        (r"\bghaliex\b", "għaliex"),
    ),
    "sq": (
        (r"\bShqiperi\b", "Shqipëri"),
    ),
}

LANGUAGE_MARKERS: dict[str, tuple[str, ...]] = {
    "de": (" der ", " die ", " das ", " und ", " ist ", " nicht ", " ich ", " wir ", " mit "),
    "sv": (" den ", " det ", " och ", " att ", " är ", " inte ", " jag ", " vi ", " med "),
    "es": (" el ", " la ", " los ", " las ", " que ", " una ", " con ", " para "),
    "fr": (" le ", " la ", " les ", " des ", " est ", " une ", " avec ", " pour "),
    "pt": (" o ", " os ", " uma ", " que ", " com ", " para ", " não "),
    "it": (" il ", " lo ", " gli ", " che ", " una ", " con ", " per "),
    "da": (" den ", " det ", " og ", " ikke ", " jeg ", " med ", " til "),
    "no": (" den ", " det ", " og ", " ikke ", " jeg ", " med ", " til "),
    "is": (" og ", " ekki ", " ég ", " með ", " til ", " sem "),
    "cs": (" ten ", " tato ", " a ", " že ", " není ", " jsem ", " pro "),
    "sk": (" ten ", " táto ", " a ", " že ", " nie ", " som ", " pre "),
    "pl": (" ten ", " ta ", " i ", " że ", " nie ", " jest ", " dla "),
    "hu": (" az ", " egy ", " és ", " nem ", " van ", " hogy "),
    "ro": (" un ", " o ", " și ", " nu ", " este ", " pentru "),
    "tr": (" bu ", " bir ", " ve ", " değil ", " için ", " ile "),
    "hr": (" ovaj ", " jedna ", " i ", " nije ", " za ", " sa "),
    "bs": (" ovaj ", " jedna ", " i ", " nije ", " za ", " sa "),
    "sr": (" ovaj ", " jedna ", " i ", " nije ", " za ", " sa "),
    "sl": (" ta ", " ena ", " in ", " ni ", " za ", " je "),
    "nl": (" de ", " het ", " een ", " en ", " niet ", " voor "),
    "fi": (" tämä ", " yksi ", " ja ", " ei ", " on ", " varten "),
    "et": (" see ", " üks ", " ja ", " ei ", " on ", " jaoks "),
    "lv": (" tas ", " viena ", " un ", " nav ", " ir ", " par "),
    "lt": (" tai ", " viena ", " ir ", " nėra ", " yra ", " už "),
    "vi": (" một ", " và ", " không ", " là ", " cho ", " của "),
    "ca": (" el ", " la ", " els ", " una ", " amb ", " per "),
    "ga": (" an ", " na ", " agus ", " ní ", " tá ", " le "),
    "mt": (" il ", " u ", " mhux ", " huwa ", " għal "),
    "sq": (" ky ", " një ", " dhe ", " nuk ", " është ", " për "),
}


def mask_technical_text(text: str) -> str:
    """Mask common technical spans while retaining line positions."""
    patterns = (
        r"```.*?```",
        r"`[^`\n]+`",
        r"https?://\S+",
        r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b",
    )
    masked = text
    for pattern in patterns:
        masked = re.sub(
            pattern,
            lambda match: "".join("\n" if char == "\n" else " " for char in match.group(0)),
            masked,
            flags=re.DOTALL,
        )
    return masked


def detect_languages(text: str) -> tuple[str, ...]:
    padded = f" {text.casefold()} "
    scores = {
        language: sum(padded.count(marker) for marker in markers)
        for language, markers in LANGUAGE_MARKERS.items()
    }
    best = max(scores.values(), default=0)
    if best == 0:
        return ()
    return tuple(language for language, score in scores.items() if score == best)


def iter_findings(text: str, languages: tuple[str, ...]):
    for language in languages:
        for pattern, suggestion in RULES[language]:
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                line = text.count("\n", 0, match.start()) + 1
                yield line, language, match.group(0), suggestion


def check_file(path: Path, language: str) -> int:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        print(f"{path}: not valid UTF-8", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"{path}: {error}", file=sys.stderr)
        return 1

    status = 0
    if text != unicodedata.normalize("NFC", text):
        print(f"{path}: Unicode text is not NFC-normalized")
        status = 1

    prose = mask_technical_text(text)
    normalized_language = language.casefold().split("-", 1)[0].split("_", 1)[0]
    if language == "auto":
        languages = detect_languages(prose)
    elif language == "all":
        languages = tuple(RULES)
    elif normalized_language not in RULES:
        languages = ()
        print(
            f"{path}: no deterministic diacritics rules for {language!r}; "
            "Unicode NFC passed, but the native-orthography review is still required."
        )
    else:
        languages = (normalized_language,)
    for line, code, found, suggestion in iter_findings(prose, languages):
        print(f"{path}:{line}: [{code}] {found!r} -> check {suggestion!r}")
        status = 1
    return status


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Flag common missing diacritics in UTF-8 prose files."
    )
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument(
        "--language",
        default="auto",
        help=(
            "target language or BCP 47 tag; unsupported languages still receive "
            "Unicode checks and exit successfully when clean; default: auto"
        ),
    )
    args = parser.parse_args()

    return max(check_file(path, args.language) for path in args.files)


if __name__ == "__main__":
    raise SystemExit(main())
