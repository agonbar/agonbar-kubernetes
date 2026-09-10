#!/usr/bin/env python3
"""Classify a ROM library by whether each title is playable in Spanish.

Reads `platform<TAB>name<TAB>crc32` on stdin, matches each CRC against the
No-Intro DATs mirrored in libretro/libretro-database, and reports one of:

  ES        your dump is playable in Spanish
  HAY-ES    it is not, but a Spanish dump of the same game exists (named)
  SOLO-EN   no Spanish dump of this game exists anywhere in the DAT
  ?         the CRC is not in the DAT at all

Two things make this harder than a name lookup, and both are handled here.

Spanish releases are often retitled ("Golden Sun - The Lost Age" ships as
"Golden Sun - La Edad Perdida"), so grouping regional variants by title misses
exactly the games that matter. The DAT's `serial` field does link them: on
Nintendo systems the last character is the region code and the prefix
identifies the game, so BPEE (USA) and BPES (Spain) group under BPE. Where a
platform carries no serials the title is normalized and used instead, which is
weaker but only has to work for single-title games.

And No-Intro only lists languages when a dump has more than one, so a bare
(Spain) is Spanish and a bare (Europe) is English. Reading a missing list as
"unknown" would file every single-language Spanish release as unclassified.

Usage:
  psql ... -tAF$'\\t' -c "select p.fs_slug, r.fs_name, r.crc_hash
                          from roms r join platforms p on p.id=r.platform_id
                          where r.crc_hash is not null" \\
    | scripts/romm-spanish-audit.py
"""
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import defaultdict

CACHE = os.path.expanduser("~/.cache/rom-dats")
BASE = ("https://raw.githubusercontent.com/libretro/libretro-database"
        "/master/metadat/{collection}/")

# Slug -> DATs to search, in order. A list because cartridge libraries get
# mixed in practice: three of the files in this library's gbc/ folder are Game
# Boy titles, so gbc has to fall through to the gb DAT or they read as unknown
# rather than as what they are.
DATS = {
    "gb": [("no-intro", "Nintendo - Game Boy")],
    "gbc": [("no-intro", "Nintendo - Game Boy Color"),
            ("no-intro", "Nintendo - Game Boy")],
    "gba": [("no-intro", "Nintendo - Game Boy Advance")],
    "nes": [("no-intro", "Nintendo - Nintendo Entertainment System")],
    "snes": [("no-intro", "Nintendo - Super Nintendo Entertainment System")],
    "sfc": [("no-intro", "Nintendo - Super Nintendo Entertainment System")],
    "n64": [("no-intro", "Nintendo - Nintendo 64")],
    "nds": [("no-intro", "Nintendo - Nintendo DS")],
    "n3ds": [("no-intro", "Nintendo - Nintendo 3DS")],
    "gamegear": [("no-intro", "Sega - Game Gear")],
    "mastersystem": [("no-intro", "Sega - Master System - Mark III")],
    "megadrive": [("no-intro", "Sega - Mega Drive - Genesis")],
    "genesis": [("no-intro", "Sega - Mega Drive - Genesis")],
    "atari2600": [("no-intro", "Atari - 2600")],
    "lynx": [("no-intro", "Atari - Lynx")],
    "ngp": [("no-intro", "SNK - Neo Geo Pocket")],
    "ngpc": [("no-intro", "SNK - Neo Geo Pocket Color")],
    "wonderswan": [("no-intro", "Bandai - WonderSwan")],
    "wonderswancolor": [("no-intro", "Bandai - WonderSwan Color")],
    "pcengine": [("no-intro", "NEC - PC Engine - TurboGrafx 16")],
    "tg16": [("no-intro", "NEC - PC Engine - TurboGrafx 16")],
    # Disc systems live in the Redump set instead.
    "psx": [("redump", "Sony - PlayStation")],
    "ps2": [("redump", "Sony - PlayStation 2")],
    "psp": [("redump", "Sony - PlayStation Portable")],
    "gamecube": [("redump", "Nintendo - GameCube")],
    "gc": [("redump", "Nintendo - GameCube")],
    "wii": [("redump", "Nintendo - Wii")],
    "dreamcast": [("redump", "Sega - Dreamcast")],
    "saturn": [("redump", "Sega - Saturn")],
}

# Disc serials are catalogue numbers that share nothing across regions
# (SLES-50123 against SLUS-20099), so the serial cannot group regional
# variants there the way a Nintendo one does. Those platforms group by title,
# which misses a release that was also retitled.
NO_SERIAL_GROUPING = {"psx", "ps2", "psp", "gamecube", "gc", "wii",
                      "dreamcast", "saturn"}

LANGS_RE = re.compile(r"^[A-Z][a-z](,[A-Z][a-z])+$")
TAGS_RE = re.compile(r"\(([^()]*)\)")


def fetch_dat(slug):
    """Return the concatenated DAT text for a slug, cached on disk."""
    sources = DATS.get(slug)
    if not sources:
        return None
    os.makedirs(CACHE, exist_ok=True)
    out = []
    for collection, name in sources:
        path = os.path.join(CACHE, f"{collection}--{name}.dat")
        if not os.path.exists(path):
            url = BASE.format(collection=collection) + urllib.parse.quote(name + ".dat")
            try:
                with urllib.request.urlopen(url, timeout=180) as r:
                    data = r.read()
            except Exception as e:
                print(f"  no pude bajar {name}: {e}", file=sys.stderr)
                continue
            with open(path, "wb") as f:
                f.write(data)
        out.append(open(path, encoding="utf-8", errors="replace").read())
    return "\n".join(out) if out else None


def parse_dat(text):
    """Yield (name, region, serial, crc) per game entry."""
    for block in text.split("game (")[1:]:
        name = re.search(r'name "([^"]+)"', block)
        crc = re.search(r"\bcrc ([0-9A-Fa-f]{8})\b", block)
        if not name or not crc:
            continue
        region = re.search(r'region "([^"]+)"', block)
        serial = re.search(r'serial "([^"]+)"', block)
        yield (name.group(1), region.group(1) if region else "",
               serial.group(1) if serial else "", crc.group(1).lower())


def spanish(name, region):
    """True when this dump can be played in Spanish."""
    tags = TAGS_RE.findall(name)
    langs = [t for t in tags if LANGS_RE.match(t)]
    if langs:
        # A language list is authoritative: it names every language present.
        return any("Es" == x for x in langs[0].split(","))
    # No list means one language, implied by the region.
    return "Spain" in region or "Spain" in " ".join(tags)


def group_key(name, serial, slug=""):
    """Key that links the same game across regions."""
    if len(serial) >= 4 and slug not in NO_SERIAL_GROUPING:
        return "serial:" + serial[:-1]      # drop the region character
    base = TAGS_RE.sub("", name).strip().lower()
    return "title:" + re.sub(r"[^a-z0-9]+", "", base)


def main():
    rows = []
    for line in sys.stdin:
        parts = line.rstrip("\n").split("\t")
        if len(parts) >= 3 and parts[2].strip():
            rows.append((parts[0].strip(), parts[1].strip(), parts[2].strip().lower()))
    if not rows:
        print("sin entradas en stdin", file=sys.stderr)
        return 1

    by_platform = defaultdict(list)
    for slug, name, crc in rows:
        by_platform[slug].append((name, crc))

    counts = defaultdict(int)
    for slug in sorted(by_platform):
        text = fetch_dat(slug)
        print(f"\n=== {slug} ({len(by_platform[slug])} ficheros) ===")
        if text is None:
            print(f"  sin DAT para «{slug}»; añádelo a DATS si existe")
            counts["?"] += len(by_platform[slug])
            continue
        entries = list(parse_dat(text))
        by_crc = {c: (n, r, s) for n, r, s, c in entries}
        # Which groups have a Spanish dump, and what it is called.
        es_by_group = {}
        for n, r, s, _ in entries:
            if spanish(n, r):
                es_by_group.setdefault(group_key(n, s, slug), n)

        for name, crc in sorted(by_platform[slug]):
            hit = by_crc.get(crc)
            if not hit:
                print(f"  ?         {name}")
                counts["?"] += 1
                continue
            dat_name, region, serial = hit
            if spanish(dat_name, region):
                print(f"  ES        {dat_name}")
                counts["ES"] += 1
                continue
            alt = es_by_group.get(group_key(dat_name, serial, slug))
            if alt:
                print(f"  HAY-ES    {dat_name}")
                print(f"            -> existe: {alt}")
                counts["HAY-ES"] += 1
            else:
                print(f"  SOLO-EN   {dat_name}")
                counts["SOLO-EN"] += 1

    print("\n=== resumen ===")
    for k in ("ES", "HAY-ES", "SOLO-EN", "?"):
        print(f"  {k:9s} {counts[k]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
