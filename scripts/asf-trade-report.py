#!/usr/bin/env python3
"""Report which Steam trading cards the ASF bots could pass to each other.

Read-only: it never sends a trade. For every game with cards in any bot's
inventory it reports, per receiving bot:

  READY   the bot already holds a full set and can craft the next badge level
  FREE    the other bots' spares complete the set (no donor loses its own set)
  COSTLY  the set only completes if a donor gives up a card it needs itself
  PARTIAL the donors' spares cover part of the missing cards

Spares are tradable copies beyond the one a donor keeps for its own next
craft. A donor whose badge is maxed (level 5, foil level 1) keeps nothing.

Sources: inventories (with the tradable flag) come from ASF's IPC API. The
card list and the current badge level come from each account's public
steamcommunity.com/profiles/<id>/gamecards/<appid> page, which ASF does not
expose. Pages are cached for 12 h in ~/.cache/asf-trade-report.

    scripts/asf-trade-report.py            full report
    scripts/asf-trade-report.py --fresh    ignore the page cache

The IPC password comes from $ASF_IPC_PASSWORD, or the games/archisteamfarm
Secret on the lamg context.
"""

import argparse
import base64
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

ASF_URL = os.environ.get("ASF_URL", "https://asf.adriangonzalezbarbosa.eu")
CACHE = Path.home() / ".cache" / "asf-trade-report"
CACHE_TTL = 12 * 3600
MAX_LEVEL = {0: 5, 1: 1}  # cardborder -> highest badge level

CARD_RE = re.compile(
    r'<div class="badge_card_set_card (?:owned|unowned)">.*?'
    r'badge_card_set_title[^>]*>(.*?)<div style="clear: right">',
    re.S,
)
LEVEL_RE = re.compile(r'<meta name="Description" content="Level (\d+) ')


def ipc_password():
    if os.environ.get("ASF_IPC_PASSWORD"):
        return os.environ["ASF_IPC_PASSWORD"]
    out = subprocess.check_output(
        ["kubectl", "--context", "lamg", "-n", "games", "get", "secret",
         "archisteamfarm", "-o", "jsonpath={.data.ipcPassword}"])
    return base64.b64decode(out).decode()


def asf_get(path, password):
    req = urllib.request.Request(f"{ASF_URL}/Api/{path}",
                                 headers={"Authentication": password})
    with urllib.request.urlopen(req, timeout=120) as r:
        body = json.load(r)
    if not body.get("Success"):
        sys.exit(f"ASF {path}: {body.get('Message')}")
    return body["Result"]


def card_name(name):
    for suffix in (" (Foil Trading Card)", " (Trading Card)", " (Foil)"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def inventory_cards(raw):
    """(appid, border) -> card name -> [total, tradable]"""
    desc = {(d["classid"], d["instanceid"]): d for d in raw["Descriptions"]}
    cards = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for a in raw["Assets"]:
        d = desc[(a["classid"], a["instanceid"])]
        tags = {t["category"]: t["internal_name"] for t in d.get("tags", [])}
        if tags.get("item_class") != "item_class_2":
            continue
        appid = int(tags["Game"].removeprefix("app_"))
        border = 1 if tags.get("cardborder") == "cardborder_1" else 0
        c = cards[(appid, border)][card_name(d["name"])]
        c[0] += a["amount"]
        if d.get("tradable"):
            c[1] += a["amount"]
    return cards


def gamecards_page(steamid, appid, border, fresh):
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{steamid}-{appid}-{border}.html"
    if not fresh and path.exists() and time.time() - path.stat().st_mtime < CACHE_TTL:
        return path.read_text()
    url = (f"https://steamcommunity.com/profiles/{steamid}/gamecards/{appid}/"
           f"?l=english{'&border=1' if border else ''}")
    for attempt in range(5):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                page = r.read().decode()
            break
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 4:
                raise
            time.sleep(30 * (attempt + 1))
    time.sleep(1.5)  # steamcommunity throttles bursts
    path.write_text(page)
    return page


def parse_page(page):
    """-> (card names in set order, current badge level)"""
    names = []
    for raw in CARD_RE.findall(page):
        text = re.sub(r"<[^>]+>.*?</div>", "", raw, flags=re.S)  # qty "(N)"
        names.append(card_name(html.unescape(re.sub(r"<[^>]+>", "", text)).strip()))
    m = LEVEL_RE.search(page)
    return names, int(m.group(1)) if m else 0


def game_title(page, appid):
    m = re.search(r'<meta property="og:title" content="[^"]*?Steam Badge: (.*?)"', page)
    return html.unescape(m.group(1)) if m else f"app {appid}"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fresh", action="store_true", help="ignore the page cache")
    args = ap.parse_args()

    password = ipc_password()
    bots = {n: b["SteamID"] for n, b in asf_get("Bot/ASF", password).items()}
    inv = {n: inventory_cards(asf_get(f"Bot/{n}/Inventory/753/6", password)[n])
           for n in bots}

    sets = sorted({k for cards in inv.values() for k in cards})
    print(f"{len(bots)} bots, {len(sets)} card sets to check", file=sys.stderr)

    found = defaultdict(list)
    unmatched = Counter()
    for i, (appid, border) in enumerate(sets, 1):
        print(f"\r{i}/{len(sets)}", end="", file=sys.stderr)
        info = {}
        for n, sid in bots.items():
            page = gamecards_page(sid, appid, border, args.fresh)
            names, level = parse_page(page)
            info[n] = (names, level, page)
        names = next((v[0] for v in info.values() if v[0]), [])
        if not names:
            continue
        title = game_title(next(iter(info.values()))[2], appid)
        if border:
            title += " (foil)"
        for n in bots:
            unmatched[n] += sum(1 for c in inv[n].get((appid, border), {}) if c not in names)

        def owned(n, c):
            return inv[n].get((appid, border), {}).get(c, [0, 0])

        def maxed(n):
            return info[n][1] >= MAX_LEVEL[border]

        def spare(n, c):
            total, tradable = owned(n, c)
            keep = 0 if maxed(n) else 1
            return max(0, min(tradable, total - keep))

        for rx in bots:
            if maxed(rx):
                continue
            missing = [c for c in names if owned(rx, c)[0] == 0]
            if not missing:
                found["READY"].append(f"{rx}: {title}, level {info[rx][1]} -> {info[rx][1] + 1}")
                continue
            donors = [d for d in bots if d != rx]
            free = {c: next((d for d in donors if spare(d, c) > 0), None) for c in missing}
            if all(free.values()):
                gifts = ", ".join(f"{c} from {d}" for c, d in free.items())
                found["FREE"].append(f"{rx}: {title}, {len(missing)} of {len(names)} cards ({gifts})")
                continue
            any_ = {c: next((d for d in donors if owned(d, c)[1] > 0), None) for c in missing}
            if all(any_.values()):
                gifts = ", ".join(
                    f"{c} from {d}{'' if spare(d, c) else ' (its only copy)'}"
                    for c, d in any_.items())
                found["COSTLY"].append(f"{rx}: {title}, {len(missing)} of {len(names)} cards ({gifts})")
                continue
            covered = [c for c, d in free.items() if d]
            if covered:
                found["PARTIAL"].append(
                    f"{rx}: {title}, spares cover {len(covered)} of {len(missing)} missing "
                    f"({', '.join(f'{c} from {free[c]}' for c in covered)})")
    print(file=sys.stderr)

    for kind in ("READY", "FREE", "COSTLY", "PARTIAL"):
        print(f"\n## {kind} ({len(found[kind])})")
        for line in found[kind]:
            print(f"- {line}")
    if any(unmatched.values()):
        print(f"\nwarning: inventory cards not matched to a set page: {dict(unmatched)}",
              file=sys.stderr)


if __name__ == "__main__":
    main()
