#!/usr/bin/env python3
"""Copy ROMs into a RomM library tree, renaming to their No-Intro name.

Reads `slug<TAB>canonical_name<TAB>source_path` and writes each file to
<dest>/roms/<slug>/<canonical_name>, verifying the CRC32 of the copy against
the source before counting it as done. Skips a file that is already present
with a matching CRC, so a rerun after an interrupted copy resumes.

The rename is the point, not tidiness: RomM reads region and language from the
filename and from nowhere else, so "Donkey Kong Country Returns [PAL][Spanish]
[Wii].iso" imports with both facets empty while the No-Intro name carries
(Europe) (En,Fr,De,Es,It) and fills them.

Usage: rom-consolidate.py <dest-root> [--apply]   (default is a dry run)
"""
import os
import shutil
import sys
import zlib


def crc(path):
    c = 0
    with open(path, "rb") as f:
        while chunk := f.read(4 << 20):
            c = zlib.crc32(chunk, c)
    return c & 0xFFFFFFFF


def main():
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 1
    dest_root = sys.argv[1]
    apply = "--apply" in sys.argv
    rows = [l.rstrip("\n").split("\t") for l in sys.stdin if l.strip()]
    total = ok = skipped = failed = 0
    for slug, name, src in rows:
        total += 1
        dst_dir = os.path.join(dest_root, "roms", slug)
        dst = os.path.join(dst_dir, name)
        if not os.path.exists(src):
            print(f"  FALTA   {src}")
            failed += 1
            continue
        size = os.path.getsize(src) / 1024**2
        if not apply:
            print(f"  [dry] {slug}/{name}  ({size:.0f} MB)")
            continue
        src_crc = crc(src)
        if os.path.exists(dst) and os.path.getsize(dst) == os.path.getsize(src):
            if crc(dst) == src_crc:
                print(f"  YA     {slug}/{name}")
                skipped += 1
                continue
        os.makedirs(dst_dir, exist_ok=True)
        tmp = dst + ".part"
        shutil.copyfile(src, tmp)
        if crc(tmp) != src_crc:
            os.remove(tmp)
            print(f"  CRC MAL {slug}/{name} — copia descartada")
            failed += 1
            continue
        os.replace(tmp, dst)
        print(f"  OK     {slug}/{name}  ({size:.0f} MB, crc {src_crc:08x})")
        ok += 1
    print(f"\n  {total} en el manifiesto: {ok} copiados, {skipped} ya estaban, "
          f"{failed} fallidos")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
