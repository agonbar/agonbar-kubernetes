#!/usr/bin/env python3
"""Drop non-Steam shortcuts whose Exe contains a substring.

Edits a copy of shortcuts.vdf. Steam must be stopped while the result goes back
in place, or it rewrites the file from memory on exit and the change is lost.
Dry run unless --apply. Needs the `vdf` package:
  nix shell --impure --expr 'with import <nixpkgs> {}; python3.withPackages (p: [ p.vdf ])'

usage: steam-shortcuts-prune.py SHORTCUTS_VDF SUBSTRING [--apply]
"""
import shutil
import sys

import vdf


def main():
    args = [a for a in sys.argv[1:] if a != "--apply"]
    path, needle = args
    data = vdf.binary_loads(open(path, "rb").read())
    entries = data["shortcuts"]
    keep = []
    for s in entries.values():
        exe = s.get("Exe", s.get("exe", ""))
        drop = needle in exe
        print(f"{'drop' if drop else 'keep'}  {s.get('AppName', s.get('appname', ''))!r}")
        if not drop:
            keep.append(s)
    # Steam indexes entries "0".."n-1"; renumber so there are no gaps.
    data["shortcuts"] = {str(i): s for i, s in enumerate(keep)}
    if "--apply" in sys.argv:
        shutil.copy2(path, path + ".bak")
        open(path, "wb").write(vdf.binary_dumps(data))
        print(f"wrote {path} ({len(entries)} -> {len(keep)}), backup at {path}.bak")
    else:
        print(f"dry run: {len(entries)} -> {len(keep)}, pass --apply to write")


if __name__ == "__main__":
    main()
