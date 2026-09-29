#!/usr/bin/env python3
"""Check that Cruncharr's Torznab answers only ever label the episode Sonarr asked for.

For each series it samples aired episodes (first, last and evenly spaced, up to 10 a season
and --per-series in total) and queries Cruncharr the way Sonarr does, from the Sonarr pod:
  t=tvsearch q=<title> season=S ep=E        (standard)
  t=search   q="<title> <abs:02>"           (anime absolute, when Sonarr has the number)
Each answer must hold 0 or 1 release, labelled with the Sonarr episode, whose CR episode
title matches the TVDB title. When the titles differ (translations), the match reasons that
Cruncharr logs must include an air-date agreement. Generic titles ("Episode 6", "TBA") can't
be checked either way and are counted apart.

Previous behaviour comes from the gap-fill report: "grabbed" with a CR title that fits the
TVDB title was a correct hit before; "grabbed" with another title, "bug:*" and the imported
mismatches were wrong or ambiguous; "not_on_cruncharr" was a miss.

Default series set: every series with a Cruncharr hit in the gap report, plus every series
in imported-mismatch.json / suspect-files.json.

Results are JSONL, one line per query, and the run is resumable.

Usage: validate-cruncharr-matcher.py [--report FILE] [--series TITLE ...] [--per-series N]
                                     [--shard i/n] [--summary-only] [--show-misses]
"""
import argparse
import base64
import collections
import difflib
import html
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

CTX, NS = "lamg", "piracy"
SONARR_POD = ["deploy/sonarr-deployment", "-c", "sonarr"]
VALIDATION = os.path.expanduser("~/Documents/projects/cruncharr/.validation")


def kexec(script, *args, stdin=None, timeout=3600):
    cmd = ["kubectl", "--context", CTX, "-n", NS, "exec", "-i", *SONARR_POD, "--",
           "sh", "-c", script, "_", *args]
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout).stdout


def sonarr(path, jq):
    # jq runs inside the pod: kubectl exec truncates stdout around 800 KB.
    for attempt in range(3):
        out = kexec(r'''K=$(grep -o "<ApiKey>[^<]*" /config/config.xml | cut -c9-)
curl -s -m 120 -H "X-Api-Key: $K" "http://localhost:8989/api/v3/$1" | jq -c "$2"''', path, jq)
        if out.strip():  # kubectl exec now and then returns nothing
            return json.loads(out)
        time.sleep(5)
    raise RuntimeError(f"Sonarr {path}: no answer")


# Reads "key<TAB>t<TAB>q<TAB>season<TAB>ep" lines, prints "key<TAB>title<TAB>title...".
TORZNAB_LOOP = r'''PW="$1"
while IFS="$(printf '\t')" read -r key t q s e; do
  if [ "$t" = tvsearch ]; then X="&season=$s&ep=$e"; else X=""; fi
  r=$(curl -s -m 60 -G "http://cruncharr:3000/api?t=$t&cat=5070$X" --data-urlencode "apikey=$PW" \
      --data-urlencode "q=$q" | grep -o '<title>[^<]*</title>' | sed 's/<[^>]*>//g' | grep -v '^cruncharr$' | tr '\n' '\t')
  printf '%s\t%s\n' "$key" "$r"
  sleep 0.3
done'''


def gui_password():
    out = subprocess.run(["kubectl", "--context", CTX, "-n", NS, "get", "secret", "cruncharr",
                          "-o", "jsonpath={.data.gui-password}"], capture_output=True, text=True).stdout
    return base64.b64decode(out).decode()


def search_title(title):
    """Sonarr's GetCleanSceneTitle, near enough: & -> and, drop `'. , other non-word -> space."""
    t = title.replace("&", "and")
    t = re.sub(r"[`'.]", "", t)
    return re.sub(r"\W+", " ", t).strip()


def norm(t):
    return re.sub(r"[^\w]+", " ", re.sub(r"[`'’]", "", (t or "").lower())).strip()


def generic(t):
    return not t or re.fullmatch(r"\s*(episode\s*\d+|ep\.?\s*\d+|tba|tbd|\d+)\s*", t, re.I) is not None


def titles_agree(a, b):
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False
    if re.findall(r"\d+", na) != re.findall(r"\d+", nb):
        return False
    return na == nb or na in nb or nb in na or difflib.SequenceMatcher(None, na, nb).ratio() >= 0.85


def sample(episodes, per_series):
    now = datetime.now(timezone.utc).isoformat()
    by_season = collections.defaultdict(list)
    for e in episodes:
        if e.get("d") and e["d"] < now:
            by_season[e["s"]].append(e)
    picks = []
    for s, eps in sorted(by_season.items()):
        eps.sort(key=lambda x: x["e"])
        n = min(10 if s else 3, len(eps))
        idx = sorted({round(i * (len(eps) - 1) / max(n - 1, 1)) for i in range(n)})
        picks.append([eps[i] for i in idx])
    # Round-robin so a 30-season series still gets first/last of every season before the middles.
    order = []
    for rank in range(10):
        for season in picks:
            ordered = [season[0], season[-1], *season[1:-1]] if len(season) > 1 else season
            if rank < len(ordered) and ordered[rank] not in order:
                order.append(ordered[rank])
    return order[:per_series]


def default_series():
    names = set()
    with open(f"{VALIDATION}/cruncharr-gap-report.jsonl") as f:
        for line in f:
            r = json.loads(line)
            if r["status"] != "not_on_cruncharr":
                names.add(r["series"])
    for fn in ("imported-mismatch.json", "suspect-files.json"):
        with open(f"{VALIDATION}/{fn}") as f:
            names.update(r["series"] for r in json.load(f))
    return names


def previous_status():
    prev = {}
    with open(f"{VALIDATION}/cruncharr-gap-report.jsonl") as f:
        for line in f:
            r = json.loads(line)
            m = re.search(r" - S\d+E\d+ - (.*) \[1080p\]", (r.get("detail") or [""])[0])
            prev[(r["series"], r["s"], r["e"])] = (r["status"], m[1] if m else None)
    for fn in ("imported-mismatch.json", "suspect-files.json"):
        with open(f"{VALIDATION}/{fn}") as f:
            for r in json.load(f):
                m = re.search(r"S(\d+)E(\d+)", r["src"])
                if m:
                    prev.setdefault((r["series"], int(m[1]), int(m[2])), ("imported_wrong_title", None))
    return prev


def follow_match_log(path):
    """Append Cruncharr's "Matched" lines to `path` while the queries run. Reading them back
    afterwards with `kubectl logs` loses most: the debug output rotates the container log
    within minutes."""
    # `logs -f` ends when the container log rotates, so keep restarting it.
    return subprocess.Popen(f"while true; do kubectl --context {CTX} -n {NS} logs -f --since=5s deploy/cruncharr-deployment "
                            f"| grep -a --line-buffered 'Matched \"' >> '{path}'; done", shell=True, start_new_session=True)


def match_reasons(path):
    """(query, season, episode) -> reasons, from the followed "Matched" log lines."""
    found = {}
    if not os.path.exists(path):
        return found
    for line in open(path, errors="replace"):
        line = re.sub(r"\x1b\[[0-9;]*m", "", line.rstrip())
        m = re.search(r'Matched "(.*)" S(\d+)E(\d+) to .* as S(\d+)E(\d+) \(sonarr=(\w[\w-]*); (.*)\)$', line)
        if m:
            found[(norm(m[1]), int(m[2]), int(m[3]))] = {"sonarr": m[6], "reasons": m[7]}
    return found


def run_queries(args, series_list):
    done = set()
    if os.path.exists(args.report):
        with open(args.report) as f:
            done = {json.loads(l)["key"] for l in f}
    pw = gui_password()
    for i, sr in enumerate(series_list, 1):
        eps = sonarr(f"episode?seriesId={sr['id']}",
                     "[.[]|{s:.seasonNumber,e:.episodeNumber,a:.absoluteEpisodeNumber,t:.title,d:.airDateUtc}]")
        by_abs = {e["a"]: e for e in eps if e.get("a")}
        q = search_title(sr["title"])
        todo = []
        for e in sample(eps, args.per_series):
            todo.append((f"{sr['id']}:tv:{e['s']}:{e['e']}", "tvsearch", q, e["s"], e["e"], e))
            if sr["seriesType"] == "anime" and e.get("a") and e["s"] > 0:
                todo.append((f"{sr['id']}:abs:{e['a']}", "search", f"{q} {e['a']:02d}", 1, e["a"], by_abs[e["a"]]))
        todo = [t for t in todo if t[0] not in done]
        print(f"[{i}/{len(series_list)}] {sr['title']}: {len(todo)} queries", file=sys.stderr)
        if not todo:
            continue

        def ask(batch):
            lines = "".join(f"{k}\t{t}\t{qq}\t{s}\t{e}\n" for k, t, qq, s, e, _ in batch)
            got = {}
            for line in kexec(TORZNAB_LOOP, pw, stdin=lines).splitlines():
                key, *titles = line.split("\t")
                got[key] = [html.unescape(t) for t in titles if t]
            return got

        got = ask(todo)
        # A cold series can exceed Cruncharr's 9s search deadline; ask the misses once more.
        misses = [t for t in todo if not got.get(t[0])]
        if misses:
            time.sleep(5)
            got.update({k: v for k, v in ask(misses).items() if v})
        lines = "".join(json.dumps({"key": key, "series": sr["title"], "type": t, "q": qq, "qs": s, "qe": e,
                                    "want": want, "releases": got.get(key, [])}) + "\n"
                        for key, t, qq, s, e, want in todo)
        with open(args.report, "a") as f:
            f.write(lines)  # one write per series, so parallel shards don't interleave lines


def evaluate(args):
    reasons = match_reasons(args.match_log)
    prev = previous_status()
    rows = [json.loads(l) for l in open(args.report)]
    per = collections.defaultdict(collections.Counter)
    problems, misses = [], []
    for r in rows:
        c = per[r["series"]]
        w = r["want"]
        c["queries"] += 1
        before, before_title = prev.get((r["series"], w["s"], w["e"]), (None, None))
        rel = r["releases"]
        # "grabbed" only means Sonarr took it; it was right only if the CR title fits.
        if before == "grabbed" and not (titles_agree(before_title, w.get("t")) or generic(before_title) or generic(w.get("t"))):
            before = "grabbed_wrong_title"
        if before:
            c["prev_known"] += 1
            if before == "grabbed":
                c["prev_correct"] += 1
            elif before != "not_on_cruncharr":
                c["prev_wrong"] += 1
        if not rel:
            c["miss"] += 1
            if before == "grabbed":
                c["lost"] += 1
                misses.append(r)
            continue
        c["hit"] += 1
        if before == "grabbed":
            c["kept"] += 1
        if len(rel) > 1:
            c["wrong"] += 1
            problems.append(("multiple releases", r))
            continue
        m = re.search(r" - S(\d+)E(\d+) - (.*) \[1080p\]", rel[0])
        if not m or (int(m[1]), int(m[2])) != (w["s"], w["e"]):
            c["wrong"] += 1
            problems.append(("label differs from Sonarr episode", r))
            continue
        cr_title = m[3]
        why = reasons.get((norm(r["q"]) if r["type"] == "tvsearch" else norm(re.sub(r"\s+\d+$", "", r["q"])),
                           r["qs"], r["qe"]), {}).get("reasons", "")
        if generic(cr_title) or generic(w.get("t")):
            c["unverifiable_generic"] += 1
        elif titles_agree(cr_title, w.get("t")):
            c["ok_title"] += 1
        elif "air-date" in why:
            c["ok_airdate"] += 1
        else:
            c["wrong"] += 1
            problems.append((f"title differs, no air-date agreement ({why or 'no log line'})", r))

    tot = collections.Counter()
    print(f"{'series':45} {'q':>4} {'hit':>4} {'ok':>4} {'date':>4} {'gen':>4} {'WRONG':>5} {'prevOK':>6} {'kept':>4} {'prevBad':>7}")
    for s in sorted(per):
        c = per[s]
        tot.update(c)
        print(f"{s[:45]:45} {c['queries']:4} {c['hit']:4} {c['ok_title']:4} {c['ok_airdate']:4} "
              f"{c['unverifiable_generic']:4} {c['wrong']:5} {c['prev_correct']:6} {c['kept']:4} {c['prev_wrong']:7}")
    print(f"\nseries={len(per)} queries={tot['queries']} hits={tot['hit']} ok_title={tot['ok_title']} "
          f"ok_airdate={tot['ok_airdate']} unverifiable_generic={tot['unverifiable_generic']} WRONG={tot['wrong']}")
    if tot["prev_correct"]:
        print(f"recall vs previous correct hits: {tot['kept']}/{tot['prev_correct']} "
              f"({100 * tot['kept'] / tot['prev_correct']:.1f}%); previously wrong/ambiguous queried: {tot['prev_wrong']}")
    for why, r in problems:
        print(f"WRONG [{why}] {r['q']} S{r['qs']}E{r['qe']} want S{r['want']['s']}E{r['want']['e']} "
              f"'{r['want'].get('t')}' got {r['releases']}")
    if args.show_misses:
        for r in misses:
            print(f"LOST {r['q']} S{r['qs']}E{r['qe']} want S{r['want']['s']}E{r['want']['e']} '{r['want'].get('t')}'")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", default=f"{VALIDATION}/matcher-validation.jsonl")
    ap.add_argument("--series", nargs="*")
    ap.add_argument("--per-series", type=int, default=40)
    ap.add_argument("--match-log", default=f"{VALIDATION}/matcher-validation.log")
    ap.add_argument("--summary-only", action="store_true")
    ap.add_argument("--shard", default="0/1", help="i/n: run every n-th series, for parallel runs on one report")
    ap.add_argument("--show-misses", action="store_true")
    args = ap.parse_args()
    if not args.summary_only:
        names = set(args.series) if args.series else default_series()
        all_series = sonarr("series", "[.[]|{id,title,seriesType}]")
        chosen = sorted((s for s in all_series if s["title"] in names), key=lambda s: s["title"])
        missing = names - {s["title"] for s in chosen}
        if missing:
            print(f"not in Sonarr: {sorted(missing)}", file=sys.stderr)
        i, n = map(int, args.shard.split("/"))
        follower = follow_match_log(args.match_log)
        try:
            run_queries(args, chosen[i::n])
        finally:
            time.sleep(3)
            os.killpg(follower.pid, 15)
    evaluate(args)


if __name__ == "__main__":
    main()
