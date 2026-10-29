#!/usr/bin/env python3
"""Import AniList watched history into Jellyfin through the Ani-Sync plugin.

Ani-Sync's manual sync (AniList -> Jellyfin) marks episodes 1..progress played,
but an entry at progress 0 becomes -1 and marks the WHOLE season. So: snapshot,
sync, then unmark what landed in the seasons of zero-progress entries.

    kubectl --context=lamg -n piracy port-forward svc/jellyfin 18096:8096 &
    scripts/jellyfin-anilist-import.py snapshot before.json
    scripts/jellyfin-anilist-import.py sync Completed      # or Watching / Both; blocks ~2 s per entry
    scripts/jellyfin-anilist-import.py diff before.json    # newly played, by series/season
    scripts/jellyfin-anilist-import.py fix-zero before.json [--apply]

Logs in as Jellyfin user agonbar with the password from the personal Bitwarden
(item "jellyfin"), piped, never printed. AniList list is public (user agonbar).
"""
import json, os, subprocess, sys, urllib.parse, urllib.request
from collections import Counter

JF = os.environ.get("JF_URL", "http://127.0.0.1:18096")
BW_ITEM = "5e4789a4-e653-468a-804c-77cb08f85c9d"
ANILIST_USER = "agonbar"
FRIBB = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"
AUTH = 'MediaBrowser Client="anilist-import", Device="work-vm-00", DeviceId="anilist-import", Version="1"'


def req(method, path, body=None, token=None, timeout=60):
    h = {"Content-Type": "application/json",
         "Authorization": AUTH + (f', Token="{token}"' if token else "")}
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.urlopen(urllib.request.Request(JF + path, data, h, method=method), timeout=timeout)
    raw = r.read()
    return json.loads(raw) if raw else None


def login():
    env = dict(os.environ, BITWARDENCLI_APPDATA_DIR=os.path.expanduser("~/.config/bw-personal"))
    session = open(os.path.expanduser("~/.config/bw-personal/session")).read().strip()
    pw = subprocess.run(["bw", "get", "password", BW_ITEM, "--session", session],
                        env=env, capture_output=True, text=True, check=True).stdout.strip()
    r = req("POST", "/Users/AuthenticateByName", {"Username": "agonbar", "Pw": pw})
    return r["AccessToken"], r["User"]["Id"]


def anime_library(tok, uid):
    views = req("GET", f"/UserViews?userId={uid}", token=tok)["Items"]
    return next(v["Id"] for v in views if v["Name"] == "Anime")


def played_episodes(tok, uid, lib):
    q = urllib.parse.urlencode({"userId": uid, "ParentId": lib, "IncludeItemTypes": "Episode",
                                "Recursive": "true", "IsPlayed": "true", "Fields": "SeasonId"})
    items = req("GET", f"/Items?{q}", token=tok, timeout=300)["Items"]
    return {i["Id"]: {"series": i.get("SeriesName"), "season": i.get("ParentIndexNumber"),
                      "seasonId": i.get("SeasonId"), "seriesId": i.get("SeriesId")} for i in items}


def anilist_entries():
    q = """query($u:String){MediaListCollection(userName:$u,type:ANIME){lists{entries{
           status progress media{id title{romaji}}}}}}"""
    r = urllib.request.urlopen(urllib.request.Request(
        "https://graphql.anilist.co", json.dumps({"query": q, "variables": {"u": ANILIST_USER}}).encode(),
        # AniList answers 403 to urllib's default User-Agent
        {"Content-Type": "application/json", "Accept": "application/json",
         "User-Agent": "jellyfin-anilist-import"}), timeout=60)
    lists = json.load(r)["data"]["MediaListCollection"]["lists"]
    return [e for l in lists for e in l["entries"]]


def main():
    cmd, args = sys.argv[1], sys.argv[2:]
    tok, uid = login()
    lib = anime_library(tok, uid)

    if cmd == "snapshot":
        eps = played_episodes(tok, uid, lib)
        json.dump(eps, open(args[0], "w"))
        print(f"{len(eps)} played episodes saved to {args[0]}")

    elif cmd == "sync":
        q = urllib.parse.urlencode({"provider": "AniList", "userId": uid, "status": args[0],
                                    "syncAction": "UpdateJellyfin"})
        req("POST", f"/AniSync/sync?{q}", token=tok, timeout=6 * 3600)
        print("sync finished")

    elif cmd == "diff":
        before = json.load(open(args[0]))
        new = {k: v for k, v in played_episodes(tok, uid, lib).items() if k not in before}
        c = Counter((v["series"], v["season"]) for v in new.values())
        for (s, n), cnt in sorted(c.items(), key=lambda x: (x[0][0] or "", x[0][1] or 0)):
            print(f"{cnt:4d}  {s}  S{n}")
        print(f"{len(new)} newly played episodes in {len(c)} seasons")

    elif cmd == "fix-zero":
        before = json.load(open(args[0]))
        apply = "--apply" in args
        zero = [e for e in anilist_entries() if e["status"] == "CURRENT" and not e["progress"]]
        fribb = {m["anilist_id"]: m for m in json.load(urllib.request.urlopen(FRIBB, timeout=120))
                 if m.get("anilist_id")}
        q = urllib.parse.urlencode({"userId": uid, "ParentId": lib, "IncludeItemTypes": "Series",
                                    "Recursive": "true", "Fields": "ProviderIds"})
        series = req("GET", f"/Items?{q}", token=tok)["Items"]
        by_tvdb = {s["ProviderIds"].get("Tvdb"): s for s in series}
        new = {k: v for k, v in played_episodes(tok, uid, lib).items() if k not in before}
        for e in zero:
            m = fribb.get(e["media"]["id"], {})
            s = by_tvdb.get(str(m.get("tvdb_id")))
            season = (m.get("season") or {}).get("tvdb")
            name = e["media"]["title"]["romaji"]
            if not s or season is None:
                print(f"skip {name}: not mapped to a Jellyfin season")
                continue
            hit = [k for k, v in new.items() if v["seriesId"] == s["Id"] and v["season"] == season]
            print(f"{name} -> {s['Name']} S{season}: {len(hit)} wrongly played")
            for k in hit if apply else []:
                req("DELETE", f"/UserPlayedItems/{k}?userId={uid}", token=tok)
        if not apply:
            print("dry run, add --apply to unmark")


if __name__ == "__main__":
    main()
