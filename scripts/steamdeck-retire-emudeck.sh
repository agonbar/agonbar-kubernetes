#!/usr/bin/env bash
# Remove EmuDeck from the Steam Deck once RetroDECK (scripts/retrodeck-romm.sh)
# and a standalone Eden (scripts/switch-yuzu-to-eden-saves.py) have taken over.
# Run this ON the Deck, dry run by default:
#   ssh deck@<steamdeck> bash -s < scripts/steamdeck-retire-emudeck.sh
#   ssh deck@<steamdeck> bash -s -- --apply < scripts/steamdeck-retire-emudeck.sh
#
# The Emulation folder was a Syncthing share with no versioning. Take it out of
# Syncthing on the Deck first, or deleting it here deletes it on every peer;
# the script refuses to run while it is still shared.
# Kept on purpose: Eden.AppImage and ~/.local/share/eden (RetroDECK dropped its
# Switch emulators), Shadps4-qt.AppImage (no PS4 in RetroDECK either), Heroic,
# sdgyrodsu (gyro for any emulator). Steam shortcuts go separately, with Steam
# stopped: scripts/steam-shortcuts-prune.py.
set -euo pipefail
shopt -s nullglob nocaseglob

EMULATION=/run/media/deck/SN01T/Emulation
SYNCTHING_CFG=~/.var/app/com.github.zocker_160.SyncThingy/.local/state/syncthing/config.xml

FLATPAKS_USER=(org.libretro.RetroArch org.ppsspp.PPSSPP)
FLATPAKS_SYSTEM=(net.pcsx2.PCSX2 net.rpcs3.RPCS3 org.mamedev.MAME)
PATHS=(
  "$EMULATION"
  ~/Applications/{DuckStation,ES-DE,EmuDeck,azahar,pcsx2-Qt,rpcs3}.AppImage
  ~/Applications/{pegasus-fe,publish}
  ~/.config/{EmuDeck,Cemu,PCSX2,Ryujinx,Vita3K,azahar-emu,citra-emu,pegasus-frontend,rpcs3,steam-rom-manager,yuzu}
  ~/.local/share/{yuzu,azahar-emu,citra-emu,duckstation}
  ~/.cache/yuzu
  ~/.var/app/{app.xemu.xemu,io.github.shiiion.primehack,net.kuribo64.melonDS,org.DolphinEmu.dolphin-emu,org.citra_emu.citra,org.duckstation.DuckStation,org.scummvm.ScummVM}
  ~/emudeck ~/ES-DE
  ~/.local/share/applications/{ES-DE,EmuDeck,Ryujinx,yuzu}.desktop
  ~/.config/systemd/user/EmuDeckCloudSync.service
  ~/.steam/steam/controller_base/templates/*emudeck*
)

if [[ -f $SYNCTHING_CFG ]] && grep -qF "path=\"$EMULATION\"" "$SYNCTHING_CFG"; then
  echo "$EMULATION is still a Syncthing folder here; remove it from Syncthing first" >&2
  exit 1
fi

existing=()
for p in "${PATHS[@]}"; do [[ -e $p ]] && existing+=("$p"); done

if [[ ${1:-} != --apply ]]; then
  for f in "${FLATPAKS_USER[@]}"; do flatpak info --user "$f" &>/dev/null && echo "flatpak --user  $f"; done
  for f in "${FLATPAKS_SYSTEM[@]}"; do flatpak info --system "$f" &>/dev/null && echo "flatpak --system $f"; done
  ((${#existing[@]})) && du -sch "${existing[@]}" 2>/dev/null
  echo "dry run, pass --apply to delete"
  exit 0
fi

systemctl --user disable --now EmuDeckCloudSync.service &>/dev/null || true
flatpak uninstall --user -y --noninteractive --delete-data "${FLATPAKS_USER[@]}" || true
# System flatpaks need polkit; over SSH that can fail without a password.
flatpak uninstall --system -y --noninteractive --delete-data "${FLATPAKS_SYSTEM[@]}" \
  || echo "system flatpaks not removed, uninstall them from Discover: ${FLATPAKS_SYSTEM[*]}"
# The first pass once left an empty tree behind in ~/.config/EmuDeck ("Directory
# not empty") while the EmuDecky plugin was still running; a second pass clears it.
((${#existing[@]})) && { rm -rf "${existing[@]}" || rm -rf "${existing[@]}"; }
systemctl --user daemon-reload
echo "removed ${#existing[@]} paths"
