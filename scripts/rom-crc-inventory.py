#!/usr/bin/env python3
"""Emit `slug<TAB>filename<TAB>crc32` for a ROM tree, for romm-spanish-audit.py.

DAT files record the CRC of the ROM itself, not of the archive around it, so a
zipped set has to be read one level in. That costs nothing: a zip stores each
entry's CRC32 in its central directory, so 5931 of the 8224 files in this
library are answered by reading a few hundred bytes each instead of
decompressing 5.6 GB.

Directory names come from ES-DE and do not match RomM or No-Intro slugs
("Gameboy Games", "Sega Mega Drive (Sega Genesis)"), so they are mapped here.

Usage:
  rom-crc-inventory.py /juegos/ROMS          # whole tree
  rom-crc-inventory.py /juegos/ROMS SNES MAME  # only these subdirectories
"""
import os
import sys
import zipfile
import zlib

# ES-DE / hand-made directory name -> RomM slug, lowercased for lookup.
SLUGS = {
    "gameboy games": "gb",
    "gameboy": "gb",
    "gameboy color": "gbc",
    "gameboy advance": "gba",
    "nintendo nes": "nes",
    "nes": "nes",
    "snes": "snes",
    "super nintendo": "snes",
    "nintendo 64": "n64",
    "nds": "nds",
    "nintendo ds": "nds",
    "sega master system": "mastersystem",
    "sega mega drive (sega genesis)": "megadrive",
    "mega drive": "megadrive",
    "atari 2600": "atari2600",
    "turbografx": "tg16",
    "zx spectrum": "zxspectrum",
    "neo geo": "neogeo",
    "mame": "mame",
    "wii": "wii",
    "psp": "psp",
    "psx": "psx",
    "ps2": "ps2",
}

# Not games: media, emulator payloads, packaging left in the set.
SKIP_EXT = {".txt", ".xml", ".cfg", ".ini", ".dat", ".png", ".jpg", ".jpeg",
            ".bmp", ".pdf", ".nfo", ".hi", ".db", ".sav", ".srm", ".state",
            ".mp4", ".avi", ".mkv", ".exe", ".dll", ".pgf", ".bat"}


def crc_of(path):
    """CRC32 of the ROM, reading into the archive when there is one."""
    if path.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(path) as z:
                # Largest entry: sets bundle the ROM with stray text files.
                items = [i for i in z.infolist() if not i.is_dir()]
                if not items:
                    return None
                return format(max(items, key=lambda i: i.file_size).CRC, "08x")
        except Exception:
            return None
    c = 0
    try:
        with open(path, "rb") as f:
            while chunk := f.read(1 << 20):
                c = zlib.crc32(chunk, c)
    except OSError:
        return None
    return format(c & 0xFFFFFFFF, "08x")


def main():
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 1
    root = sys.argv[1]
    only = {a.lower() for a in sys.argv[2:]}
    n = skipped = 0
    for entry in sorted(os.listdir(root)):
        d = os.path.join(root, entry)
        if not os.path.isdir(d):
            continue
        if only and entry.lower() not in only:
            continue
        slug = SLUGS.get(entry.lower())
        if not slug:
            print(f"# sin slug para «{entry}», saltada", file=sys.stderr)
            continue
        for dirpath, _, files in os.walk(d):
            for fn in sorted(files):
                if os.path.splitext(fn)[1].lower() in SKIP_EXT:
                    skipped += 1
                    continue
                crc = crc_of(os.path.join(dirpath, fn))
                if crc:
                    print(f"{slug}\t{fn}\t{crc}")
                    n += 1
                else:
                    skipped += 1
    print(f"# {n} hasheados, {skipped} saltados", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
