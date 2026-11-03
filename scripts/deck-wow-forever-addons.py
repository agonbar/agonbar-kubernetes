#!/usr/bin/env python3
"""Install a light, gamepad-first addon set for WoW: Forever on the Steam Deck.

Run FROM work-vm-00. For every addon it takes the newest CurseForge file tagged
with a 1.60.x game version (Forever's flavor), unpacks it and syncs it into the
Deck's _forever_/Interface/AddOns. It also seeds WTF with the gamepad CVars and
the ConsolePort/DialogueUI/... settings from the retail install on the VM-TINY11
disk (/mnt/win11, the SATA controller is passed through to work-vm-00).

    TMPDIR=/mnt/strata deck-wow-forever-addons.py [--host 192.168.1.218] [--dry-run]

Staging takes about 4 GB with the Spoken packs; work-vm-00's root sits near
kubelet's 15 % free eviction line, hence TMPDIR on the strata disk.

Rerun it any time to pick up addon updates. WTF files are only written when the
Deck does not have them yet, so in-game changes are never overwritten.

Why this set: ConsolePort is the controller layer the user already plays retail
with (3.3.13+ supports Forever and hides the native gamepad bar itself). The
rest is what the user runs on retail that has a Forever build and costs little,
plus EnhanceQoL, which the user wants and which does not clash with ConsolePort.
Left out on purpose: OPie
(ConsolePort Rings does it), Clique (mouse click-casting), RareScanner and
Narcissus (large databases), Auctionator's and Syndicator's retail data (other
economy, other characters). No Forever build on 2026-11-03: TSM, Kaliel's
Tracker, World Quest Tab, LittleWigs, Better Timeline.

Spoken reads quests, zones and books aloud in Castilian Spanish (it follows the
client language, esES here). The player and the books pack are on CurseForge;
the esES quest and zone packs are only published as GitHub releases of
rusty-key/spoken-wow, about 1.8 GB between them.
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ADDONS = {  # CurseForge project ids, taken from the retail WowUp addons.json
    "ConsolePort": 91376,
    "DialogueUI": 989270,
    "Plumber": 888580,
    "Baganator": 914482,
    "Syndicator": 988370,
    "Auctionator": 6124,
    "TomTom": 18808,
    "WaypointUI": 1275146,
    "BigWigs": 2382,
    "EnhanceQoL": 1076354,
    "Spoken": 1700375,
    "SpokenBooksEsES": 1723280,
}
GITHUB_REPO = "rusty-key/spoken-wow"
GITHUB_PACKS = {  # name -> release tag prefix; the newest matching release wins
    "SpokenQuestsEsES": "quests-audio-esES/",
    "SpokenZonesEsES": "zones-audio-esES/",
}
FOREVER_VERSION = "1.60"

RETAIL = Path("/mnt/win11/Program Files (x86)/World of Warcraft/_retail_")
ACCOUNT = "411663972#1"
SAVED_VARIABLES = [
    "ConsolePort.lua", "ConsolePort_Cursor.lua", "ConsolePort_Keyboard.lua",
    "ConsolePort_Rings.lua", "consoleport_bar.lua", "DialogueUI.lua",
    "Plumber.lua", "Baganator.lua", "TomTom.lua", "WaypointUI.lua", "BigWigs.lua", "EnhanceQoL.lua",
]
# Low graphics to get past the first load under Proton (the one Linux report
# needed the slider at 1); raise it in game once it runs.
EXTRA_CVARS = {"graphicsQuality": "1", "InputDeviceInterfaceStyle": "1"}

FOREVER = ("/home/deck/.local/share/Steam/steamapps/compatdata/2792117172/pfx/"
           "drive_c/Program Files (x86)/World of Warcraft/_forever_")
SSH = ["ssh", "-i", str(Path.home() / ".ssh/nas"), "-o", "IdentitiesOnly=yes",
       "-o", "BatchMode=yes", "-o", "HostKeyAlias=192.168.1.218"]


def get(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
                                  timeout=60)


def cf(path):
    return get("https://www.curseforge.com/api/v1/" + path)


def unzip(url, dest):
    """Stream the zip to disk (the Spoken packs are over a GB) and unpack it."""
    with tempfile.TemporaryFile() as tmp:
        shutil.copyfileobj(get(url), tmp, 1 << 20)
        with zipfile.ZipFile(tmp) as z:
            z.extractall(dest)
            return sorted({n.split("/")[0] for n in z.namelist() if "/" in n})


def fetch(name, pid, dest):
    files = json.load(cf(f"mods/{pid}/files?pageIndex=0&pageSize=50&sort=dateCreated"
                         "&sortDescending=true&removeAlphas=true"))["data"]
    hit = next((f for f in files if any(v.startswith(FOREVER_VERSION) for v in f["gameVersions"])), None)
    if not hit:
        sys.exit(f"{name}: no file tagged {FOREVER_VERSION}.x on CurseForge")
    dirs = unzip(f"https://www.curseforge.com/api/v1/mods/{pid}/files/{hit['id']}/download", dest)
    print(f"{name:12} {hit['displayName']:28} {', '.join(dirs)}")
    return dirs


def fetch_github(name, prefix, dest):
    releases = json.load(get(f"https://api.github.com/repos/{GITHUB_REPO}/releases?per_page=100"))
    rel = max((r for r in releases if r["tag_name"].startswith(prefix)), key=lambda r: r["published_at"])
    asset = next(a for a in rel["assets"] if a["name"].endswith(".zip"))
    dirs = unzip(asset["browser_download_url"], dest)
    print(f"{name:12} {rel['tag_name']:28} {', '.join(dirs)}")
    return dirs


def config_wtf(current):
    """Add the retail GamePad* CVars and EXTRA_CVARS that the Deck's Config.wtf
    does not set yet; whatever the game already wrote wins."""
    have = {l.split()[1] for l in current.splitlines() if l.startswith("SET ")}
    lines = [l for l in (RETAIL / "WTF/Config.wtf").read_text(errors="replace").splitlines()
             if l.startswith("SET GamePad")]
    lines += [f'SET {k} "{v}"' for k, v in EXTRA_CVARS.items()]
    new = [l for l in lines if l.split()[1] not in have]
    return current + "".join(l + "\n" for l in new), new


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.1.218")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    deck = SSH + [f"deck@{a.host}"]

    if subprocess.run(deck + ["pgrep -f '[_]forever_.Wow.exe'"], capture_output=True).returncode == 0:
        msg = "WoW: Forever is running on the Deck; close it first (it rewrites WTF on exit)"
        if not a.dry_run:
            sys.exit(msg)
        print("warning:", msg)

    stage = Path(tempfile.mkdtemp(prefix="wow-forever-"))
    addons = stage / "Interface/AddOns"
    dirs = [d for n, p in ADDONS.items() for d in fetch(n, p, addons)]
    dirs += [d for n, p in GITHUB_PACKS.items() for d in fetch_github(n, p, addons)]

    wtf = stage / "WTF"
    sv = wtf / "Account" / ACCOUNT / "SavedVariables"
    sv.mkdir(parents=True)
    for f in SAVED_VARIABLES:
        if (RETAIL / "WTF/Account" / ACCOUNT / "SavedVariables" / f).exists():
            shutil.copy2(RETAIL / "WTF/Account" / ACCOUNT / "SavedVariables" / f, sv)
    # ConsolePort's button layout lives in the account keybindings.
    shutil.copy2(RETAIL / "WTF/Account" / ACCOUNT / "bindings-cache.wtf", sv.parent)

    rsh = " ".join(SSH)
    dry = ["--dry-run"] if a.dry_run else []
    target = f"deck@{a.host}:{FOREVER}/"
    subprocess.run(deck + [f"mkdir -p '{FOREVER}/Interface/AddOns' '{FOREVER}/WTF'"], check=True)
    # Addon folders are replaced whole so files dropped upstream go away; other
    # folders in AddOns are left alone.
    for d in dirs:
        subprocess.run(["rsync", "-a", "--delete", *dry, "-e", rsh,
                        f"{addons / d}/", f"{target}Interface/AddOns/{d}/"], check=True)
    subprocess.run(["rsync", "-a", "--ignore-existing", "-v", *dry, "-e", rsh,
                    f"{wtf}/", f"{target}WTF/"], check=True)
    cfg = f"'{FOREVER}/WTF/Config.wtf'"
    current = subprocess.run(deck + [f"cat {cfg} 2>/dev/null"], capture_output=True, text=True).stdout
    merged, new = config_wtf(current)
    print(f"Config.wtf: {len(new)} CVars added" + (f" ({', '.join(l.split()[1] for l in new)})" if new else ""))
    if new and not a.dry_run:
        subprocess.run(deck + [f"cat > {cfg}"], input=merged, text=True, check=True)
    shutil.rmtree(stage)
    print(f"{len(dirs)} addon folders synced to {a.host}")


if __name__ == "__main__":
    main()
