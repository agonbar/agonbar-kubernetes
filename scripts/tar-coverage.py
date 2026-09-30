#!/usr/bin/env python3
"""Which files in a backup tar have no byte-identical copy anywhere else?

Reads `sha256sum` output (lines "HASH  PATH") on stdin, collected from the
places that might already hold the data, and hashes every regular file in each
tar. Prints a per-top-directory summary and the members with no match, so a
local-only backup can be dropped or kept knowingly.

usage: ssh host 'find DIRS -type f -exec sha256sum {} +' | tar-coverage.py A.tar [B.tar.gz ...]
"""
import collections
import hashlib
import sys
import tarfile


def main():
    known = {}
    for line in sys.stdin:
        digest, _, path = line.rstrip("\n").partition("  ")
        if len(digest) == 64:
            known.setdefault(digest, path)

    for archive in sys.argv[1:]:
        total = collections.Counter()
        covered = collections.Counter()
        missing = []
        with tarfile.open(archive) as tar:
            for member in tar:
                if not member.isfile():
                    continue
                digest = hashlib.sha256(tar.extractfile(member).read()).hexdigest()
                group = "/".join(member.name.split("/")[:6])
                total[group] += 1
                if digest in known:
                    covered[group] += 1
                else:
                    missing.append((member.size, member.name))
        print(f"== {archive}: {sum(covered.values())}/{sum(total.values())} files have a copy elsewhere")
        for group in sorted(total):
            print(f"  {covered[group]:4}/{total[group]:<4} {group}")
        for size, name in sorted(missing):
            print(f"  only here: {size:>10}  {name}")


if __name__ == "__main__":
    main()
