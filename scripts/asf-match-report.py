#!/usr/bin/env python3
"""Compare achievements and playtime between the ASF bots, game by game.

Read-only: it prints the ASF commands that would even the accounts out and
runs none of them.

    scripts/asf-match-report.py "Final Fantasy"     games matching a name
    scripts/asf-match-report.py 1173790,1173800     explicit appids

Games are found with ASF's `owns`, across all bots, so a game only one
account owns still shows up. A game borrowed through Steam Families is not
owned: the borrower appears in the comparison, but `play` may not be able to
run it there.

Achievements come from `alist` (the ASFAchievementManager plugin). Playtime
comes from the public steamcommunity.com/profiles/<id>/gamecards/<appid> page,
which only shows it for games with trading cards.

For each game and bot it prints:
  aset <bot> <appid> <n,...>   achievements another bot has and this one lacks
  play <bot> <appid>           with the hours to idle, to reach the top account

`play` idles until `resume <bot>`, which also restarts card farming, so the
stop has to be timed by hand.

The IPC password comes from $ASF_IPC_PASSWORD, or the games/archisteamfarm
Secret on the lamg context.
"""

import base64
import json
import os
import re
import subprocess
import sys
import urllib.request

ASF_URL = os.environ.get("ASF_URL", "https://asf.adriangonzalezbarbosa.eu")


def ipc_password():
    if os.environ.get("ASF_IPC_PASSWORD"):
        return os.environ["ASF_IPC_PASSWORD"]
    out = subprocess.check_output(
        ["kubectl", "--context", "lamg", "-n", "games", "get", "secret",
         "archisteamfarm", "-o", "jsonpath={.data.ipcPassword}"])
    return base64.b64decode(out).decode()


def asf(password, path, command=None):
    data = json.dumps({"Command": command}).encode() if command else None
    req = urllib.request.Request(
        f"{ASF_URL}/Api/{path}", data=data,
        headers={"Authentication": password, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        body = json.load(r)
    if not body.get("Success"):
        sys.exit(f"ASF {path} {command or ''}: {body.get('Message')}")
    return body["Result"]


def per_bot(text):
    """Split a multi-bot command response into bot -> its lines."""
    out, bot = {}, None
    for line in text.splitlines():
        m = re.match(r"<(\w+)>\s?(.*)", line)
        if m:
            bot = m.group(1)
            line = m.group(2)
        if bot:
            out.setdefault(bot, []).append(line)
    return out


def hours(steamid, appid):
    url = f"https://steamcommunity.com/profiles/{steamid}/gamecards/{appid}/?l=english"
    with urllib.request.urlopen(url, timeout=60) as r:
        page = r.read().decode()
    m = re.search(r"([\d,.]+) hrs on record", page)
    return float(m.group(1).replace(",", "")) if m else 0.0


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    query = sys.argv[1]
    password = ipc_password()
    bots = {n: b["SteamID"] for n, b in asf(password, "Bot/ASF").items()}

    games = {}
    for m in re.finditer(r"app/(\d+) \| (.+)", asf(password, "Command", f"owns ASF {query}")):
        games.setdefault(int(m.group(1)), m.group(2).strip())
    if re.fullmatch(r"[\d,]+", query):
        for appid in map(int, query.split(",")):
            games.setdefault(appid, f"app {appid}")
    if not games:
        sys.exit(f"no bot owns anything matching {query!r}")

    ids = ",".join(map(str, games))
    achieved = {}  # (bot, appid) -> {n: (unlocked, name)}
    for bot, lines in per_bot(asf(password, "Command", f"alist ASF {ids}")).items():
        appid = None
        for line in lines:
            m = re.search(r"Achievements for (\d+)", line)
            if m:
                appid = int(m.group(1))
                continue
            m = re.match(r"\s*(\d+)\s+\[(.)\]\s+(.*)", line)
            if m and appid:
                achieved.setdefault((bot, appid), {})[int(m.group(1))] = (
                    m.group(2) == "✅", m.group(3).strip())

    for appid, name in sorted(games.items(), key=lambda g: g[1]):
        print(f"\n## {name} (app {appid})")
        played = {b: hours(sid, appid) for b, sid in bots.items()}
        top = max(played.values())
        for bot in bots:
            ach = achieved.get((bot, appid), {})
            got = {n for n, (ok, _) in ach.items() if ok}
            others = set().union(*(
                {n for n, (ok, _) in achieved.get((o, appid), {}).items() if ok}
                for o in bots if o != bot))
            print(f"- {bot}: {len(got)}/{len(ach)} achievements, {played[bot]:.1f} h")
            if others - got:
                print(f"    aset {bot} {appid} {','.join(map(str, sorted(others - got)))}")
            if top - played[bot] >= 0.5:
                print(f"    play {bot} {appid}   # {top - played[bot]:.1f} h to idle")


if __name__ == "__main__":
    main()
