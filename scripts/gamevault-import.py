#!/usr/bin/env python3
"""Move the PC game collection out of _ADRIAN and into the GameVault library.

The collection at /mnt/RAID/adrian/_ADRIAN/Juegos/PC is 137GB of accumulated
downloads: loose ISOs, repack folders, installed games, cracks and empty
directories. GameVault reads the filename as metadata and wants
"Title (Version) (Year).ext", so nothing in there is servable as-is.

This does not rename in place and it does not symlink. It copies the servable
files into the library under canonical names, verifies them, and only then can
the source be removed — the goal is for _ADRIAN to end up empty except for what
could not be made to work.

Copy-then-verify-then-delete, in that order and as separate commands, on
purpose. The two paths are different ZFS datasets (RAID/adrian and RAID/docker)
so this is a real copy, not a rename, and a half-finished copy that already
deleted its source is unrecoverable.

Both paths live on the NAS and neither is reachable from a workstation, so
everything runs in a helper pod that mounts both. Created on first use and left
running; `--stop` removes it.

Workflow:
  gamevault-import.py audit                 # what is servable, what is not
  gamevault-import.py propose > map.tsv     # guess canonical names via IGDB
  # ...review and fix map.tsv by hand, it is meant to be edited...
  gamevault-import.py copy map.tsv          # copy in, skipping what is done
  gamevault-import.py pack packmap.tsv      # zip whole dirs into one archive
  gamevault-import.py verify map.tsv        # sizes match at both ends
  gamevault-import.py prune map.tsv         # delete the sources (needs rw)

The map is TSV: source path relative to the collection root, then the canonical
filename. Lines starting with # are ignored, so a candidate can be parked by
commenting it out rather than deleting the line.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse

CTX = "lamg"
NS = "piracy"
POD = "gamevault-import"

EXPORT = "/mnt/RAID/adrian/_ADRIAN/Juegos"
SUBPATH = "PC"
LIBRARY = "/mnt/RAID/docker/gamevault-library"

SRC = "/src"
LIB = "/lib2"

ARCHIVE_EXT = {"zip", "rar", "7z", "iso", "mdf", "img", "bin", "cab",
               "tar", "gz", "exe"}

# Below this a file is a crack, a patch, a no-cd key or cover art.
MIN_BYTES = 20 * 1024 * 1024

POD_SPEC = f"""
apiVersion: v1
kind: Pod
metadata:
  name: {POD}
  namespace: {NS}
spec:
  restartPolicy: Never
  nodeSelector:
    svccontroller.k3s.cattle.io/lbpool: lamg
  containers:
    - name: helper
      image: alpine:3.20
      # zip is what `pack` needs and alpine does not ship it. Everything else
      # here (cp, stat, find, dd) is in busybox either way.
      command: ["sh", "-c", "apk add --no-cache zip >/dev/null && sleep 86400"]
      volumeMounts:
        - name: src
          mountPath: {SRC}
          subPath: {SUBPATH}
          readOnly: true
        - name: lib
          mountPath: {LIB}
  volumes:
    - name: src
      nfs:
        server: 192.168.0.29
        path: {EXPORT}
        readOnly: true
    - name: lib
      nfs:
        server: 192.168.0.29
        path: {LIBRARY}
"""

# Release-group tags, site stamps and media markers that must not end up in the
# title handed to IGDB. Order matters: the site URLs go before the bracket
# stripping, or the brackets take the URL with them and leave a bare "www".
NOISE = [
    r"\bwww\.[a-z0-9.-]+\b", r"\[[^\]]*\]", r"\([^)]*\)",
    r"\b(rld|codex|hi2u|wmt|theta|elamigos|igg|ns|plaza|skidrow|reloaded)\b",
    r"\b(multi\d+|multi|spanish|english|espanol|repack|cracked|crack|full|"
    r"read\.nfo|nfo|setup|disk|disc|cd\d*|dvd\d*|pc|iso|v?\d+\.\d[\d.]*)\b",
]


def kubectl(*args, **kw):
    return subprocess.run(["kubectl", "--context", CTX, "-n", NS, *args],
                          capture_output=True, text=True, **kw)


def sh(script, check=True):
    r = kubectl("exec", POD, "--", "sh", "-c", script)
    if check and r.returncode != 0:
        sys.exit(f"pod command failed:\n{r.stderr.strip()}")
    return r.stdout


def ensure_pod(need_zip=False):
    if kubectl("get", "pod", POD).returncode == 0:
        # A pod left over from an earlier run may predate the alpine image, in
        # which case zip is missing and `pack` would fail halfway through a
        # multi-gigabyte archive rather than up front.
        if need_zip and kubectl("exec", POD, "--", "sh", "-c",
                                "command -v zip").returncode != 0:
            sys.exit(f"the running {POD} pod has no zip. Finish or stop any "
                     f"copy in flight, then `--stop` and re-run.")
        return
    r = subprocess.run(["kubectl", "--context", CTX, "apply", "-f", "-"],
                       input=POD_SPEC, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"could not create helper pod:\n{r.stderr.strip()}")
    # Runs as root, and the source export does not squash root, which is how it
    # reads a tree owned by 3000:3000 with mode 770.
    kubectl("wait", "--for=condition=Ready", f"pod/{POD}", "--timeout=180s")


def human(n):
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024


def scan():
    """Every file in the collection, as (size, relative path)."""
    ensure_pod()
    out = sh(f'cd {SRC} && find . -type f -exec stat -c "%s|%n" {{}} \\; 2>/dev/null')
    files = []
    for line in out.splitlines():
        if "|" not in line:
            continue
        size_s, path = line.split("|", 1)
        path = path[2:] if path.startswith("./") else path
        try:
            files.append((int(size_s), path))
        except ValueError:
            continue
    return files


def candidates(files):
    """Servable files: big enough, and an extension GameVault can serve."""
    out = []
    for size, path in files:
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext in ARCHIVE_EXT and size >= MIN_BYTES:
            out.append((size, path, ext))
    return sorted(out, reverse=True)


def cmd_audit(_a):
    files = scan()
    cands = candidates(files)
    served = {p.split("/")[0] for _, p, _ in cands}
    tops = set(sh(f"ls -1 {SRC}").splitlines())

    print("# SERVABLE — one line per file that could become a GameVault entry")
    print("# size\text\tsource_path")
    for size, path, ext in cands:
        print(f"{human(size):>7}\t{ext}\t{path}")

    print(f"\n# {len(cands)} servable files, "
          f"{human(sum(s for s, _, _ in cands))} total")
    print(f"\n# NOT SERVABLE — {len(tops - served)} top-level entries with no "
          f"single archive to serve.")
    print("# Installed games need packing into an archive first; the rest is "
          "cracks, mods and empty folders.")
    for t in sorted(tops - served):
        n = sum(1 for _, p in files if p.split("/")[0] == t)
        sz = sum(s for s, p in files if p.split("/")[0] == t)
        kind = "empty" if n == 0 else ("installed game" if n > 20 else "loose files")
        print(f"#   {t}\t{n} files\t{human(sz)}\t{kind}")


def clean_title(path):
    """Best guess at a real game title, from a repack-styled path."""
    stem = os.path.basename(path).rsplit(".", 1)[0]
    parent = path.split("/")[0]
    # A folder name is usually closer to the title than a release-group
    # filename like "rld-tww2", so prefer it when the stem looks like a tag.
    name = parent if (len(stem) < 12 and "/" in path) else stem
    name = name.replace("_", " ").replace(".", " ")
    for pat in NOISE:
        name = re.sub(pat, " ", name, flags=re.I)
    name = re.sub(r"[-–]+", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def gv_token():
    pw = subprocess.run(
        ["kubectl", "--context", CTX, "-n", NS, "get", "secret", "gamevault",
         "-o", "jsonpath={.data.admin-password}"],
        capture_output=True, text=True).stdout
    import base64
    pw = base64.b64decode(pw).decode()
    r = kubectl("exec", "deploy/gamevault", "--", "curl", "-s", "-u",
                f"agonbar:{pw}", "http://localhost:8080/api/auth/basic/login")
    try:
        return json.loads(r.stdout)["access_token"]
    except Exception:
        sys.exit("could not log in to GameVault to reach IGDB")


def igdb(token, query):
    q = urllib.parse.quote(query)
    r = kubectl("exec", "deploy/gamevault", "--", "curl", "-s", "-H",
                f"Authorization: Bearer {token}",
                f"http://localhost:8080/api/metadata/providers/igdb/search?query={q}")
    try:
        res = json.loads(r.stdout)
        return res if isinstance(res, list) else []
    except Exception:
        return []


def cmd_propose(_a):
    cands = candidates(scan())
    token = gv_token()
    print("# Generated by gamevault-import.py propose. REVIEW BEFORE USE.")
    print("# The title comes from IGDB's first match on a cleaned-up filename,")
    print("# which is a guess, not an identification. Multi-disc sets and")
    print("# compilations are where it will be wrong.")
    print("# Comment a line out to leave that file in _ADRIAN.")
    print("#\n# source_path\tcanonical_name")
    for size, path, ext in cands:
        guess = clean_title(path)
        hits = igdb(token, guess) if guess else []
        if hits:
            title = hits[0].get("title") or guess
            year = str(hits[0].get("release_date") or "")[:4]
        else:
            title, year = guess or os.path.basename(path), ""
        # Round brackets are reserved for GameVault's flags, and the filename
        # is parsed on them, so they cannot survive inside a title.
        title = title.replace("(", "[").replace(")", "]").replace("/", "-")
        name = f"{title} ({year}).{ext}" if year else f"{title}.{ext}"
        mark = "" if hits else "# NO IGDB MATCH: "
        print(f"{mark}{path}\t{name}")


def read_map(path):
    pairs, seen = [], {}
    for n, line in enumerate(open(path), 1):
        line = line.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "\t" not in line:
            sys.exit(f"{path}:{n}: expected two tab-separated columns")
        src, name = (x.strip() for x in line.split("\t", 1))
        if "/" in name:
            sys.exit(f"{path}:{n}: link name must not contain '/': {name}")
        if name in seen:
            sys.exit(f"{path}:{n}: duplicate name {name!r}, also on line {seen[name]}")
        seen[name] = n
        pairs.append((src, name))
    return pairs


def sizes_at(root, names):
    """Size of each name under root, missing ones absent from the dict."""
    if not names:
        return {}
    script = "; ".join(
        f'stat -c "%s|%n" "{root}/{n}" 2>/dev/null' for n in names)
    out = sh(script, check=False)
    res = {}
    for line in out.splitlines():
        if "|" in line:
            s, p = line.split("|", 1)
            res[os.path.relpath(p, root)] = int(s)
    return res


def cmd_copy(args):
    pairs = read_map(args.map)
    src_sizes = sizes_at(SRC, [s for s, _ in pairs])
    missing = [s for s, _ in pairs if s not in src_sizes]
    if missing:
        sys.exit("source files not found:\n  " + "\n  ".join(missing))

    done = sizes_at(LIB, [n for _, n in pairs])
    todo = [(s, n) for s, n in pairs
            if done.get(n) != src_sizes[s]]

    total = sum(src_sizes[s] for s, _ in todo)
    print(f"{len(pairs) - len(todo)} already copied, {len(todo)} to go, "
          f"{human(total)} to move")
    if args.dry_run or not todo:
        for s, n in todo:
            print(f"  would copy {human(src_sizes[s]):>8}  {s}  ->  {n}")
        return

    for i, (s, n) in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {human(src_sizes[s]):>8}  {n} ... ",
              end="", flush=True)
        # Copy to a temp name and rename, so an interrupted copy is never
        # picked up by GameVault's indexer as a truncated game.
        r = kubectl("exec", POD, "--", "sh", "-c",
                    f'cp "{SRC}/{s}" "{LIB}/.part-{i}" && '
                    f'chmod 644 "{LIB}/.part-{i}" && '
                    f'mv "{LIB}/.part-{i}" "{LIB}/{n}"')
        if r.returncode != 0:
            sh(f'rm -f "{LIB}/.part-{i}"', check=False)
            sys.exit(f"FAILED\n{r.stderr.strip()}")
        print("ok")
    cmd_verify(args)


# Content that is already compressed. Re-compressing an ISO gains nothing and
# costs an hour, so a directory made mostly of these gets zipped with -0.
PACKED_EXT = {"iso", "mdf", "rar", "7z", "zip", "cab", "bin", "gz", "mp4",
              "ogg", "png", "jpg", "jar"}


def cmd_pack(args):
    """Zip whole directories into single servable archives.

    GameVault serves one file per game version. A multi-part installer, a
    CD1/CD2/CD3 set and an already-installed game all have the same problem —
    the game is a directory, not a file — and the same fix.
    """
    pairs = read_map(args.map)
    ensure_pod(need_zip=True)

    plans = []
    for src, name in pairs:
        out = sh(f'find "{SRC}/{src}" -type f -exec stat -c "%s|%n" {{}} \\; '
                 f'2>/dev/null', check=False)
        total = packed = 0
        for line in out.splitlines():
            if "|" not in line:
                continue
            s, p = line.split("|", 1)
            s = int(s)
            total += s
            if p.rsplit(".", 1)[-1].lower() in PACKED_EXT:
                packed += s
        if total == 0:
            sys.exit(f"no files under {src!r} — wrong path?")
        level = "-0" if packed > total / 2 else "-1"
        plans.append((src, name, total, level))

    done = sizes_at(LIB, [n for _, n, _, _ in plans])
    todo = [p for p in plans if p[1] not in done]
    print(f"{len(plans) - len(todo)} already packed, {len(todo)} to go")
    for src, name, total, level in todo:
        mode = "store" if level == "-0" else "deflate"
        print(f"  {human(total):>8}  {mode:<8} {src}  ->  {name}")
    if args.dry_run or not todo:
        return

    for i, (src, name, total, level) in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {human(total):>8}  {name} ... ",
              end="", flush=True)
        # cd into the parent so paths inside the zip are relative to the game
        # directory rather than carrying /src/ down the tree.
        r = kubectl("exec", POD, "--", "sh", "-c",
                    f'cd "{SRC}" && zip {level} -r -q "{LIB}/.pack-{i}.zip" "{src}" && '
                    f'chmod 644 "{LIB}/.pack-{i}.zip" && '
                    f'mv "{LIB}/.pack-{i}.zip" "{LIB}/{name}"')
        if r.returncode != 0:
            sh(f'rm -f "{LIB}/.pack-{i}.zip"', check=False)
            sys.exit(f"FAILED\n{r.stderr.strip()}")
        # Reading the central directory back proves the archive is complete;
        # a truncated zip has no readable index.
        chk = kubectl("exec", POD, "--", "sh", "-c",
                      f'unzip -l "{LIB}/{name}" | tail -1')
        print(f"ok ({chk.stdout.strip()})")


def cmd_verify(args):
    pairs = read_map(args.map)
    src_sizes = sizes_at(SRC, [s for s, _ in pairs])
    lib_sizes = sizes_at(LIB, [n for _, n in pairs])
    ok = bad = 0
    for s, n in pairs:
        if lib_sizes.get(n) is None:
            print(f"  MISSING   {n}")
            bad += 1
        elif lib_sizes[n] != src_sizes.get(s):
            print(f"  SIZE DIFF {n}: src={src_sizes.get(s)} lib={lib_sizes[n]}")
            bad += 1
        else:
            ok += 1
    print(f"verified {ok}/{len(pairs)}" + (f", {bad} bad" if bad else ""))
    return 1 if bad else 0


def cmd_prune(args):
    """Delete sources that are already verified in the library."""
    pairs = read_map(args.map)
    src_sizes = sizes_at(SRC, [s for s, _ in pairs])
    lib_sizes = sizes_at(LIB, [n for _, n in pairs])
    safe = [s for s, n in pairs
            if s in src_sizes and lib_sizes.get(n) == src_sizes[s]]
    unsafe = [s for s, n in pairs
              if s in src_sizes and lib_sizes.get(n) != src_sizes[s]]

    if unsafe:
        print(f"{len(unsafe)} not verified, will NOT be touched:")
        for s in unsafe:
            print(f"  {s}")
    if not safe:
        print("nothing safe to delete")
        return 0

    freed = sum(src_sizes[s] for s in safe)
    print(f"{len(safe)} sources verified in the library, {human(freed)} to free")
    if args.dry_run:
        for s in safe:
            print(f"  would delete {s}")
        return 0

    # The source export is read-only by design; this refuses rather than
    # silently doing nothing, because a failed delete here looks like success.
    probe = kubectl("exec", POD, "--", "sh", "-c", f'touch {SRC}/.wtest 2>&1')
    if probe.returncode != 0 or "Read-only" in probe.stdout:
        sys.exit(f"{SRC} is mounted read-only. Set the NFS export for\n"
                 f"{EXPORT} to rw before pruning, then set it back.")
    sh(f'rm -f {SRC}/.wtest', check=False)

    for s in safe:
        sh(f'rm -f "{SRC}/{s}"')
    print(f"deleted {len(safe)} sources, freed {human(freed)}")


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--stop", action="store_true", help="delete the helper pod")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("audit").set_defaults(fn=cmd_audit)
    sub.add_parser("propose").set_defaults(fn=cmd_propose)
    for name, fn in (("copy", cmd_copy), ("pack", cmd_pack),
                     ("verify", cmd_verify), ("prune", cmd_prune)):
        s = sub.add_parser(name)
        s.add_argument("map")
        s.add_argument("-n", "--dry-run", action="store_true")
        s.set_defaults(fn=fn)

    args = p.parse_args()
    if args.stop:
        kubectl("delete", "pod", POD, "--ignore-not-found", "--wait=false")
        return 0
    if not args.cmd:
        p.print_help()
        return 2
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
