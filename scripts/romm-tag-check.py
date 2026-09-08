#!/usr/bin/env python3
"""Report the region and language RomM would read from ROM filenames.

Reimplements RomM's FSRomsHandler.parse_tags (backend/handler/filesystem/
roms_handler.py) and its REGIONS/LANGUAGES tables (.../base_handler.py) so a
library can be checked before importing it. RomM only reads tags inside ( ) or
[ ], and only accepts the codes and full names in those two tables: "EUR",
"PAL", "MULTi5" and "ESP" are not among them and parse as plain tags.

Reads filenames on stdin, one per line.

  find /path/to/roms -type f | scripts/romm-tag-check.py
  scripts/romm-tag-check.py --verbose < names.txt   # per-file detail
"""
import re
import sys
from collections import Counter

LANGUAGES = (
    ("Ar", "Arabic"), ("Da", "Danish"), ("De", "German"), ("El", "Greek"),
    ("En", "English"), ("Es", "Spanish"), ("Fi", "Finnish"), ("Fr", "French"),
    ("It", "Italian"), ("Ja", "Japanese"), ("Ko", "Korean"), ("Nl", "Dutch"),
    ("No", "Norwegian"), ("Pl", "Polish"), ("Pt", "Portuguese"),
    ("Ru", "Russian"), ("Sr", "Serbian"), ("Sv", "Swedish"),
    ("Zh", "Chinese"), ("nolang", "No Language"),
)
REGIONS = (
    ("A", "Australia"), ("AS", "Asia"), ("B", "Brazil"), ("C", "Canada"),
    ("CH", "China"), ("E", "Europe"), ("F", "France"), ("FN", "Finland"),
    ("G", "Germany"), ("GR", "Greece"), ("H", "Holland"), ("HK", "Hong Kong"),
    ("I", "Italy"), ("J", "Japan"), ("K", "Korea"), ("NL", "Netherlands"),
    ("NO", "Norway"), ("PD", "Public Domain"), ("R", "Russia"), ("S", "Spain"),
    ("SW", "Sweden"), ("T", "Taiwan"), ("U", "USA"), ("UK", "England"),
    ("UNK", "Unknown"), ("UNL", "Unlicensed"), ("W", "World"),
)

REGIONS_BY_SHORTCODE = dict(REGIONS)
LANGUAGES_BY_SHORTCODE = dict(LANGUAGES)
_REGION_BY_ALIAS = {**{n.lower(): n for _, n in REGIONS},
                    **{c.lower(): n for c, n in REGIONS}}
_LANGUAGE_BY_ALIAS = {**{n.lower(): n for _, n in LANGUAGES},
                      **{c.lower(): n for c, n in LANGUAGES}}

GENERIC_TAG_REGEX = re.compile(r"\(([^)]+)\)|\[([^]]+)\]")
VERSION_TAG_REGEX = re.compile(r"^(?:v|version)[\s|-]?(.*)$", re.I)
REGION_TAG_REGEX = re.compile(r"^reg[\s|-](.*)$", re.I)
REVISION_TAG_REGEX = re.compile(r"^(?:rev|revision)[\s|-]?(.*)$", re.I)


def parse_tags(fs_name):
    tags = [chunk.strip()
            for tag in (m[0] or m[1] for m in GENERIC_TAG_REGEX.findall(fs_name))
            for chunk in tag.split(",")]
    regions, languages, other = [], [], []
    for raw in tags:
        if raw in REGIONS_BY_SHORTCODE:
            regions.append(REGIONS_BY_SHORTCODE[raw]); continue
        if raw in LANGUAGES_BY_SHORTCODE:
            languages.append(LANGUAGES_BY_SHORTCODE[raw]); continue
        if (r := _REGION_BY_ALIAS.get(raw.strip().lower())):
            regions.append(r); continue
        if (l := _LANGUAGE_BY_ALIAS.get(raw.strip().lower())):
            languages.append(l); continue
        if VERSION_TAG_REGEX.match(raw):
            continue
        if (m := REGION_TAG_REGEX.match(raw)) and m[1].strip():
            regions.append(_REGION_BY_ALIAS.get(m[1].strip().lower()) or m[1].strip())
            continue
        if REVISION_TAG_REGEX.match(raw):
            continue
        other.append(raw)
    return regions, languages, other


def main():
    verbose = "--verbose" in sys.argv
    total = no_region = no_language = 0
    lost = Counter()
    for line in sys.stdin:
        name = line.strip().rsplit("/", 1)[-1]
        if not name:
            continue
        total += 1
        regions, languages, other = parse_tags(name)
        if not regions:
            no_region += 1
        if not languages:
            no_language += 1
        # A tag that looks regional but isn't in RomM's tables is the common
        # cause of a miss, so surface those rather than every stray tag.
        for tag in other:
            if re.fullmatch(r"[A-Za-z]{2,6}\d?", tag):
                lost[tag] += 1
        if verbose:
            print(f"{name}\n  region={regions or '-'} lang={languages or '-'} "
                  f"otros={other or '-'}")
    if not total:
        return
    print(f"\n{total} ficheros")
    print(f"  sin region:  {no_region:5d}  ({100*no_region//total}%)")
    print(f"  sin idioma:  {no_language:5d}  ({100*no_language//total}%)")
    if lost:
        print("\netiquetas no reconocidas mas frecuentes:")
        for tag, n in lost.most_common(15):
            print(f"  {n:5d}  {tag}")


if __name__ == "__main__":
    main()
