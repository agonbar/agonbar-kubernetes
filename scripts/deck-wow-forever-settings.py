#!/usr/bin/env python3
"""Seed WoW: Forever's WTF on the Steam Deck with the user's retail settings.

Run FROM work-vm-00: the retail install is on the VM-TINY11 disk, mounted at
/mnt/win11 here (its SATA controller is passed through to work-vm-00). Copies
the addons' account SavedVariables and the account keybindings (where
ConsolePort keeps its button layout), and adds the retail GamePad* CVars to
Config.wtf. Nothing the Deck already has is overwritten.

    deck-wow-forever-settings.py [--host 192.168.1.218] [--dry-run]

The addons themselves are installed and updated by the decky-wow-forever
plugin (~/Documents/projects/decky-wow-forever), from Game Mode.
"""
import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

RETAIL = Path("/mnt/win11/Program Files (x86)/World of Warcraft/_retail_")
ACCOUNT = "411663972#1"
SAVED_VARIABLES = [
    "ConsolePort.lua", "ConsolePort_Cursor.lua", "ConsolePort_Keyboard.lua",
    "ConsolePort_Rings.lua", "consoleport_bar.lua", "DialogueUI.lua",
    "Plumber.lua", "Baganator.lua", "TomTom.lua", "WaypointUI.lua", "BigWigs.lua", "EnhanceQoL.lua",
]
# Low graphics to get past the first load under Proton (the one Linux report
# needed the slider at 1); only applies if the game has not set it yet.
EXTRA_CVARS = {"graphicsQuality": "1", "InputDeviceInterfaceStyle": "1"}

FOREVER = ("/home/deck/.local/share/Steam/steamapps/compatdata/2792117172/pfx/"
           "drive_c/Program Files (x86)/World of Warcraft/_forever_")
SSH = ["ssh", "-i", str(Path.home() / ".ssh/nas"), "-o", "IdentitiesOnly=yes",
       "-o", "BatchMode=yes", "-o", "HostKeyAlias=192.168.1.218"]


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
    wtf = stage / "WTF"
    sv = wtf / "Account" / ACCOUNT / "SavedVariables"
    sv.mkdir(parents=True)
    for f in SAVED_VARIABLES:
        if (RETAIL / "WTF/Account" / ACCOUNT / "SavedVariables" / f).exists():
            shutil.copy2(RETAIL / "WTF/Account" / ACCOUNT / "SavedVariables" / f, sv)
    shutil.copy2(RETAIL / "WTF/Account" / ACCOUNT / "bindings-cache.wtf", sv.parent)

    dry = ["--dry-run"] if a.dry_run else []
    subprocess.run(deck + [f"mkdir -p '{FOREVER}/WTF'"], check=True)
    subprocess.run(["rsync", "-a", "--ignore-existing", "-v", *dry, "-e", " ".join(SSH),
                    f"{wtf}/", f"deck@{a.host}:{FOREVER}/WTF/"], check=True)
    cfg = f"'{FOREVER}/WTF/Config.wtf'"
    current = subprocess.run(deck + [f"cat {cfg} 2>/dev/null"], capture_output=True, text=True).stdout
    merged, new = config_wtf(current)
    print(f"Config.wtf: {len(new)} CVars added" + (f" ({', '.join(l.split()[1] for l in new)})" if new else ""))
    if new and not a.dry_run:
        subprocess.run(deck + [f"cat > {cfg}"], input=merged, text=True, check=True)
    shutil.rmtree(stage)


if __name__ == "__main__":
    main()
