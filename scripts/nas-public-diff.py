#!/usr/bin/env python3
"""Classify the files that exist in one Public tree and not in another.

Inputs are NUL-separated `find -printf '%s\t%T@\t%P\0'` listings:

  ssh nas03 "cd /mnt/RAID/Public && find . -type f -printf '%s\t%T@\t%P\0'" > nas03.lst
  ssh nas02 "cd /mnt/RAID/Public && find . -type f -printf '%s\t%T@\t%P\0'" > nas02.lst
  ./nas-public-diff.py nas03.lst nas02.lst [EXTRA.lst ...] [--dump DIR]

EXTRA listings (e.g. the rest of nas02's pool, with `%p` absolute paths) only
count for name+size matches, to catch files moved out of Public into personal
datasets.

For every file of A whose path is missing in B it answers "was it moved or
deduplicated on B, or does it exist nowhere in B?":

  moved      same basename + size somewhere else in B
  same-size  same size (>= 1 MiB) somewhere in B, different name: hash to confirm
  dup-in-A   another path in A has the same basename + size (a copy inside A)
  unique     none of the above

Also reports path matches whose size differs. --dump writes one file list per
class, for hashing the candidates.
"""
import collections
import os
import sys

MIB = 1 << 20


def load(path):
    files = {}
    with open(path, "rb") as f:
        for rec in f.read().split(b"\0"):
            if not rec:
                continue
            size, mtime, p = rec.decode("utf-8", "surrogateescape").split("\t", 2)
            files[p] = (int(size), float(mtime))
    return files


def human(n):
    for unit in "BKMGT":
        if n < 1024 or unit == "T":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024


def top(p):
    return p.split("/", 1)[0] if "/" in p else "(root)"


def main():
    args = sys.argv[1:]
    dump = None
    if "--dump" in args:
        i = args.index("--dump")
        dump = args[i + 1]
        del args[i:i + 2]
    a_path, b_path, extra_paths = args[0], args[1], args[2:]
    a, b = load(a_path), load(b_path)
    b_all = dict(b)
    for e in extra_paths:
        b_all.update(load(e))

    b_by_name_size = collections.defaultdict(list)
    b_by_size = collections.defaultdict(list)
    for p, (s, _) in b_all.items():
        b_by_name_size[(os.path.basename(p), s)].append(p)
        b_by_size[s].append(p)
    a_by_name_size = collections.defaultdict(list)
    for p, (s, _) in a.items():
        a_by_name_size[(os.path.basename(p), s)].append(p)

    classes = collections.defaultdict(list)  # class -> [(path, size, match)]
    size_diff = []
    for p, (s, m) in a.items():
        if p in b:
            if b[p][0] != s:
                size_diff.append((p, s, b[p][0]))
            continue
        key = (os.path.basename(p), s)
        if b_by_name_size.get(key):
            classes["moved"].append((p, s, b_by_name_size[key][0]))
        elif s >= MIB and b_by_size.get(s):
            classes["same-size"].append((p, s, b_by_size[s][0]))
        elif len(a_by_name_size[key]) > 1:
            other = next(q for q in a_by_name_size[key] if q != p)
            classes["dup-in-A"].append((p, s, other))
        else:
            classes["unique"].append((p, s, ""))

    print(f"A {a_path}: {len(a)} files {human(sum(s for s, _ in a.values()))}")
    print(f"B {b_path}: {len(b)} files {human(sum(s for s, _ in b.values()))}")
    for e in extra_paths:
        print(f"  + {e} (name+size matches only)")
    print(f"same path, different size: {len(size_diff)}")
    for p, sa, sb in sorted(size_diff)[:10]:
        print(f"    {p}  A={human(sa)} B={human(sb)}")
    for cls in ("moved", "same-size", "dup-in-A", "unique"):
        rows = classes[cls]
        print(f"\n== {cls}: {len(rows)} files {human(sum(r[1] for r in rows))}")
        per_top = collections.Counter()
        for p, s, _ in rows:
            per_top[top(p)] += s
        for t, s in per_top.most_common(15):
            n = sum(1 for p, _, _ in rows if top(p) == t)
            print(f"    {human(s):>8}  {n:>7}  {t}")
        for p, s, match in sorted(rows, key=lambda r: -r[1])[:5]:
            print(f"      e.g. {p} ({human(s)})" + (f"  ~ {match}" if match else ""))
    if dump:
        os.makedirs(dump, exist_ok=True)
        for cls, rows in classes.items():
            with open(os.path.join(dump, f"{cls}.tsv"), "w", errors="surrogateescape") as f:
                for p, s, match in rows:
                    f.write(f"{s}\t{p}\t{match}\n")


if __name__ == "__main__":
    main()
