#!/usr/bin/env python3
"""Find monitored, aired, missing Sonarr episodes that Cruncharr can serve, and queue them.

Scope (default): series on the "Anime-1080p-Spanish-Subs" profile that already have at
least one file, i.e. the partially downloaded ones. --all-series drops the file filter.

Per (series, season): probe the Cruncharr Torznab endpoint directly with the first missing
episode. A miss is retried once after a pause, because Cruncharr answers empty when a cold
lookup exceeds its 9s deadline. If the season still misses it is recorded as not on CR and
skipped. Otherwise every missing episode of that season is probed. Since cruncharr 549e701 a
miss means "not on CR, or not sure it's the right episode", not only the former.

Before cruncharr f4de443 (2026-09-29) search labelled OVAs and other seasons' episodes with
the requested SxxEyy, and Sonarr imported them. Cruncharr now checks the TVDB title and air
date against Sonarr itself and returns at most one release, so the only guard kept here is
that exactly one distinct release carries the tag.

With --grab, each probe hit is sent to Sonarr through POST /release/push. Sonarr runs it
through the same decision engine as a search result and grabs it if approved; a rejection,
or a mapping to another episode, is recorded as a suspected bug. Interactive search
(GET /release) would give the same verdict but queries every indexer, about 2 min an episode.

The report is JSONL, one line per episode, and the run is resumable: episodes already in the
report are skipped.

--fix-imported audits what is already on disk instead. For every episode whose current file
came from Cruncharr and whose CR episode title differs from the TVDB title, it asks Cruncharr
again. Same CR episode back: the titles are just translated differently, keep the file.
A different CR episode back: the file is the wrong episode, so with --apply it deletes the file
and pushes the new release. Nothing back is reported as wrong_no_replacement and left alone,
unless --drop-unreplaceable: then the file is deleted too and Sonarr searches all indexers.
Cruncharr releases are never blocklisted: the download hash is sha1(service:crEpisodeId), so
blocking a mislabelled release also blocks that CR episode where it legitimately belongs.

Usage: sonarr-cruncharr-gap-fill.py [--grab] [--all-series] [--report FILE] [--series TITLE]
       sonarr-cruncharr-gap-fill.py --fix-imported [--apply] [--report FILE] [--series TITLE]
"""
import argparse
import base64
import difflib
import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

CTX, NS = "lamg", "piracy"
PROFILE = "Anime-1080p-Spanish-Subs"
SONARR_POD = ["deploy/sonarr-deployment", "-c", "sonarr"]
CRUNCHARR_INDEXER, CRUNCHARR_CLIENT = 18, 4


def kexec(script, *args, timeout=900):
    cmd = ["kubectl", "--context", CTX, "-n", NS, "exec", "-i", *SONARR_POD, "--",
           "sh", "-c", script, "_", *args]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except subprocess.TimeoutExpired:
        return ""


SONARR_SH = r'''
K=$(grep -o "<ApiKey>[^<]*" /config/config.xml | cut -c9-)
if [ -n "$3" ]; then
  curl -s -m 600 -X "$1" -H "X-Api-Key: $K" -H "Content-Type: application/json" -d "$3" "http://localhost:8989/api/v3/$2"
else
  curl -s -m 600 -X "$1" -H "X-Api-Key: $K" "http://localhost:8989/api/v3/$2" | jq -c "${4:-.}"
fi'''


def sonarr(method, path, body=None, jq="."):
    # jq runs inside the pod: kubectl exec truncates stdout around 800 KB.
    out = kexec(SONARR_SH, method, path, json.dumps(body) if body is not None else "", jq)
    return json.loads(out) if out.strip() else None


TORZNAB_SH = r'''curl -s -m 60 -G "http://cruncharr:3000/api?t=tvsearch&cat=5070&season=$3&ep=$4" \
  --data-urlencode "apikey=$1" --data-urlencode "q=$2"'''


def torznab_items(pw, title, s, e):
    out = kexec(TORZNAB_SH, pw, f"{title} S{s:02d}E{e:02d}", str(s), str(e), timeout=120)
    try:
        return [{"title": i.findtext("title"), "link": i.findtext("link")} for i in ET.fromstring(out).iter("item")]
    except ET.ParseError:
        return []


def query_numbers(ep):
    """Sonarr searches, and parses release titles, in scene numbering when the series has an
    XEM mapping, and cruncharr (7cdcd05) labels releases the same way. Query with it too."""
    return {"qs": ep.get("sceneSeasonNumber") or ep["seasonNumber"],
            "qe": ep.get("sceneEpisodeNumber") or ep["episodeNumber"]}


def probe(pw, title, s, e):
    tag = f"S{s:02d}E{e:02d}"
    for attempt in range(2):
        hits = [i for i in torznab_items(pw, title, s, e) if tag in i["title"]]
        if hits:
            return hits
        if attempt == 0:
            time.sleep(12)
    return []


GENERIC = re.compile(r"^(episode \d+|tba|tbd|)$")


def norm(t):
    return re.sub(r"[^a-z0-9 ]+", " ", t.lower()).split()


def cr_episode_title(release):
    m = re.search(r" - S\d+E\d+ - (.*) \[1080p\]", release)
    return m.group(1) if m else ""


def titles_match(tvdb, cr):
    a, b = norm(tvdb), norm(cr)
    if GENERIC.match(" ".join(a)) or GENERIC.match(" ".join(b)):
        return True
    overlap = len(set(a) & set(b)) / max(1, min(len(set(a)), len(set(b))))
    return overlap >= 0.5 or difflib.SequenceMatcher(None, " ".join(a), " ".join(b)).ratio() >= 0.6


def check_hits(hits):
    distinct = sorted({h["title"] for h in hits})
    if len(distinct) > 1:
        return "bug:cruncharr_ambiguous", distinct
    return None, None


def grab(ep, item):
    res = sonarr("POST", "release/push", {
        "title": item["title"], "magnetUrl": item["link"], "protocol": "torrent",
        "publishDate": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "indexer": "Cruncharr", "indexerId": CRUNCHARR_INDEXER, "downloadClientId": CRUNCHARR_CLIENT})
    r = (res or [None])[0]
    if r is None:
        return "grab_failed", [item["title"]]
    if ep["e"] not in (r.get("mappedEpisodeNumbers") or []) or ep["s"] != r.get("mappedSeasonNumber"):
        return "bug:sonarr_maps_elsewhere", [item["title"], f"S{r.get('mappedSeasonNumber')}E{r.get('mappedEpisodeNumbers')}"]
    if not r.get("approved"):
        return "bug:sonarr_rejects", [item["title"], *r.get("rejections", [])]
    return "grabbed", [item["title"]]


def imported_mismatches(only_series):
    """Episodes whose current file is a Cruncharr release titled differently from TVDB."""
    rows, page = {}, 1
    while True:
        recs = sonarr("GET", f"history?eventType=3&pageSize=250&page={page}&includeEpisode=true&includeSeries=true",
                      jq='[.records[] | {src: .sourceTitle, ep: (.episode | {id, seasonNumber, episodeNumber, sceneSeasonNumber, sceneEpisodeNumber, title, hasFile, episodeFileId, monitored}), series: .series.title, sm: .series.monitored}]')
        for r in recs:
            ep = r["ep"] or {}
            if r["src"].startswith("[Cruncharr]") and ep.get("hasFile") and ep["id"] not in rows \
                    and (not only_series or r["series"] == only_series):
                rows[ep["id"]] = {"id": ep["id"], "s": ep["seasonNumber"], "e": ep["episodeNumber"], **query_numbers(ep),
                                  "name": ep.get("title") or "", "efid": ep["episodeFileId"],
                                  "src": r["src"], "series": r["series"],
                                  "monitored": r["sm"] and ep.get("monitored")}
        if len(recs) < 250:
            break
        page += 1
    efids, scene = [r["efid"] for r in rows.values()], {}
    for i in range(0, len(efids), 200):  # one request for all ids overflows the URL
        ids = "&".join(f"episodeFileIds={x}" for x in efids[i:i + 200])
        scene |= {f["id"]: f.get("sceneName") for f in sonarr("GET", f"episodefile?{ids}", jq="[.[] | {id, sceneName}]")}
    return [r for r in rows.values()
            if scene.get(r["efid"]) == r["src"] and not titles_match(r["name"], cr_episode_title(r["src"]))]


def fix_imported(a, pw, record):
    suspects = imported_mismatches(a.series)
    print(f"{len(suspects)} imported Cruncharr files titled differently from TVDB", file=sys.stderr)
    unreplaced = []
    for ep in sorted(suspects, key=lambda r: (r["series"], r["s"], r["e"])):
        hits = probe(pw, ep["series"], ep["qs"], ep["qe"])
        if not hits and a.drop_unreplaceable and a.apply and ep["monitored"]:
            sonarr("DELETE", f"episodefile/{ep['efid']}")
            unreplaced.append(ep["id"])
            record(ep, ep["series"], "dropped_searching_all_indexers", [ep["src"], f"TVDB: {ep['name']}"])
        elif not hits:
            record(ep, ep["series"], "wrong_no_replacement", [ep["src"], f"TVDB: {ep['name']}"])
        elif len({h["title"] for h in hits}) > 1:
            record(ep, ep["series"], "bug:cruncharr_ambiguous", sorted({h["title"] for h in hits}))
        elif cr_episode_title(hits[0]["title"]) == cr_episode_title(ep["src"]):
            record(ep, ep["series"], "same_episode_translated", [ep["src"], f"TVDB: {ep['name']}"])
        elif not ep["monitored"]:  # Sonarr rejects pushes for it, the file would just be lost
            record(ep, ep["series"], "unmonitored_skip", [ep["src"], hits[0]["title"], f"TVDB: {ep['name']}"])
        elif not a.apply:
            record(ep, ep["series"], "would_replace", [ep["src"], hits[0]["title"], f"TVDB: {ep['name']}"])
        else:
            sonarr("DELETE", f"episodefile/{ep['efid']}")
            status, detail = grab(ep, hits[0])
            record(ep, ep["series"], f"replaced:{status}", [ep["src"], *detail])
    if unreplaced:
        sonarr("POST", "command", {"name": "EpisodeSearch", "episodeIds": unreplaced})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--grab", action="store_true")
    ap.add_argument("--all-series", action="store_true")
    ap.add_argument("--series", help="only this series title")
    ap.add_argument("--fix-imported", action="store_true")
    ap.add_argument("--apply", action="store_true", help="with --fix-imported: delete wrong files and re-queue")
    ap.add_argument("--drop-unreplaceable", action="store_true",
                    help="with --fix-imported --apply: also delete wrong files Cruncharr has no replacement for, "
                         "and have Sonarr search every indexer for them")
    ap.add_argument("--report", default="cruncharr-gap-report.jsonl")
    a = ap.parse_args()

    pw = base64.b64decode(subprocess.run(
        ["kubectl", "--context", CTX, "-n", NS, "get", "secret", "cruncharr",
         "-o", "jsonpath={.data.gui-password}"], capture_output=True, text=True, check=True).stdout).decode()

    rep = open(a.report, "a")

    def record(ep, title, status, detail):
        row = {"id": ep["id"], "series": title, "s": ep["s"], "e": ep["e"], "status": status, "detail": detail}
        rep.write(json.dumps(row, ensure_ascii=False) + "\n")
        rep.flush()
        print(f"{title} S{ep['s']:02d}E{ep['e']:02d}: {status} {detail[:1]}", file=sys.stderr)

    if a.fix_imported:
        fix_imported(a, pw, record)
        return

    profile_id = next(p["id"] for p in sonarr("GET", "qualityprofile") if p["name"] == PROFILE)
    series = {s["id"]: s for s in sonarr("GET", "series", jq="[.[] | {id, title, qualityProfileId, statistics}]")
              if s["qualityProfileId"] == profile_id
              and (a.all_series or s["statistics"]["episodeFileCount"] > 0)
              and (not a.series or s["title"] == a.series)}

    missing, page = [], 1
    while True:
        recs = sonarr("GET", f"wanted/missing?pageSize=200&page={page}&monitored=true",
                      jq="[.records[] | {id, seriesId, seasonNumber, episodeNumber, sceneSeasonNumber, sceneEpisodeNumber, title}]")
        missing += recs
        if len(recs) < 200:
            break
        page += 1

    queued = {e for q in sonarr("GET", "queue?pageSize=2000", jq="[.records[] | {episodeId}]")
              for e in [q.get("episodeId")] if e}
    done = set()
    if os.path.exists(a.report):
        with open(a.report) as f:
            done = {json.loads(l)["id"] for l in f if l.strip()}

    seasons = {}
    for m in missing:
        if m["seriesId"] in series and m["seasonNumber"] > 0 and m["id"] not in queued and m["id"] not in done:
            seasons.setdefault((m["seriesId"], m["seasonNumber"]), []).append(
                {"id": m["id"], "s": m["seasonNumber"], "e": m["episodeNumber"], "name": m.get("title") or "",
                 **query_numbers(m)})
    print(f"{sum(map(len, seasons.values()))} episodes in {len(seasons)} seasons", file=sys.stderr)

    for (sid, s), eps in sorted(seasons.items(), key=lambda kv: (series[kv[0][0]]["title"], kv[0][1])):
        title = series[sid]["title"]
        eps.sort(key=lambda x: x["e"])
        first = probe(pw, title, eps[0]["qs"], eps[0]["qe"])
        if not first:
            for ep in eps:
                record(ep, title, "not_on_cruncharr", [])
            continue
        for ep in eps:
            hits = first if ep is eps[0] else probe(pw, title, ep["qs"], ep["qe"])
            bad, why = check_hits(hits) if hits else (None, None)
            if not hits:
                record(ep, title, "not_on_cruncharr", [])
            elif bad:
                record(ep, title, bad, why)
            elif not a.grab:
                record(ep, title, "on_cruncharr", [h["title"] for h in hits])
            else:
                status, detail = grab(ep, hits[0])
                record(ep, title, status, detail)


if __name__ == "__main__":
    main()
