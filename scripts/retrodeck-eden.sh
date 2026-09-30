#!/usr/bin/env bash
# Launch Switch games from RetroDECK's ES-DE with a standalone Eden. Run this
# ON the target machine (Steam Deck or Bazzite) after scripts/retrodeck-romm.sh:
#   ssh deck@<steamdeck> bash -s < scripts/retrodeck-eden.sh
#
# RetroDECK dropped Switch for good in Feb 2026, but its ES-DE find rules still
# look for Eden at /var/data/retrodeck/external_components/eden/, which is how
# RetroDECK itself shipped it: the AppImage extracted, run inside the sandbox.
# Extracting is what makes it work. A packed AppImage needs FUSE, which the
# sandbox lacks; that is why pointing ES-DE at the .AppImage just closes.
# The only other piece is a custom system that turns the Eden command back on.
#
# Idempotent: re-run after replacing ~/Applications/Eden.AppImage.
set -euo pipefail

APPIMAGE=${APPIMAGE:-$HOME/Applications/Eden.AppImage}
FLATPAK_ID=net.retrodeck.retrodeck
COMPONENT="$HOME/.var/app/$FLATPAK_ID/data/retrodeck/external_components/eden"
CUSTOM_SYSTEMS="$HOME/retrodeck/ES-DE/custom_systems/es_systems.xml"
CUSTOM_FIND_RULES="$HOME/retrodeck/ES-DE/custom_systems/es_find_rules.xml"

step() { printf '\n== %s\n' "$*"; }

step "extract $APPIMAGE"
stamp="$(stat -c %Y "$APPIMAGE")"
if [[ -f "$COMPONENT/.appimage-mtime" ]] && [[ "$(cat "$COMPONENT/.appimage-mtime")" == "$stamp" ]]; then
  echo "already extracted"
else
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  ( cd "$tmp" && "$APPIMAGE" --appimage-extract >/dev/null )
  rm -rf "$COMPONENT"
  mkdir -p "$(dirname "$COMPONENT")"
  # uruntime AppImages extract to AppDir and leave squashfs-root as a symlink
  # to it; move the real directory, not the link.
  mv "$(readlink -f "$tmp/squashfs-root")" "$COMPONENT"
  echo "$stamp" > "$COMPONENT/.appimage-mtime"
fi

step "launcher"
cat > "$COMPONENT/component_launcher.sh" <<'LAUNCHER'
#!/bin/bash
# Written by scripts/retrodeck-eden.sh. The extracted AppImage (sharun) brings
# its own loader, libs and Qt, so drop RetroDECK's library and Qt paths, and
# point XDG at the host defaults so this and the standalone Eden shortcut share
# keys, firmware, saves and settings.
unset LD_LIBRARY_PATH QT_PLUGIN_PATH QT_QPA_PLATFORM_PLUGIN_PATH
export XDG_CONFIG_HOME="$HOME/.config" XDG_DATA_HOME="$HOME/.local/share" XDG_CACHE_HOME="$HOME/.cache"

# A RomM game with several files (base + update + DLC) is downloaded as a
# "<title>.m3u" directory holding every NSP plus a playlist, and ES-DE hands
# over the playlist, which Eden can't open. Pass the base NSP instead: the first
# entry whose title ID ends in 000 (updates end in 800, DLC elsewhere) or has no
# title ID at all. Eden applies the update and DLC from its external content dir.
rom="${!#}"
if [[ $rom == *.m3u && -f $rom && $(stat -c %s "$rom") -lt 65536 ]]; then
  while IFS= read -r entry; do
    entry=${entry%$'\r'}
    [[ -z $entry || $entry == \#* ]] && continue
    [[ $entry =~ \[([0-9A-Fa-f]{16})\] && ${BASH_REMATCH[1]^^} != *000 ]] && continue
    set -- "${@:1:$#-1}" "$(dirname "$rom")/$entry"
    break
  done < "$rom"
fi
exec "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/AppRun" "$@"
LAUNCHER
chmod +x "$COMPONENT/component_launcher.sh"

step "custom system"
if grep -qF '.m3u .M3U' "$CUSTOM_SYSTEMS" 2>/dev/null; then
  echo "already set"
else
  # The shipped file is only a commented template; keep it next to ours.
  [[ -f "$CUSTOM_SYSTEMS" ]] && cp -n "$CUSTOM_SYSTEMS" "$CUSTOM_SYSTEMS.orig"
  cat > "$CUSTOM_SYSTEMS" <<'XML'
<?xml version="1.0"?>
<!-- Written by scripts/retrodeck-eden.sh: the stock switch system with the
     Eden command enabled. Eden lives in external_components/eden. .m3u is
     here because the RomM fork names every RomM folder game "<title>.m3u": a
     directory for several files, a plain file for one. Without it ES-DE skips
     them at startup and the game shows as not downloaded again. -->
<systemList>
    <system>
        <name>switch</name>
        <fullname>Nintendo Switch</fullname>
        <path>%ROMPATH%/switch</path>
        <extension>.m3u .M3U .nca .NCA .nro .NRO .nso .NSO .nsp .NSP .xci .XCI</extension>
        <command label="Eden (Standalone)">%EMULATOR_EDEN% -f -g %ROM%</command>
        <platform>switch</platform>
        <theme>switch</theme>
    </system>
</systemList>
XML
  echo "wrote $CUSTOM_SYSTEMS"
fi

step "custom find rule"
# The bundled rules list EDEN twice and the first wins: upstream's
# ~/Applications/Eden*.AppImage. ES-DE expands ~ to its --home, which RetroDECK
# sets to ~/.var/app/<id>/config, so that never matches, and RetroDECK's own
# external_components entry is dropped as a repeat. Custom rules load first.
if grep -qF 'external_components/eden/component_launcher.sh' "$CUSTOM_FIND_RULES" 2>/dev/null; then
  echo "already set"
else
  [[ -f "$CUSTOM_FIND_RULES" ]] && cp -n "$CUSTOM_FIND_RULES" "$CUSTOM_FIND_RULES.orig"
  cat > "$CUSTOM_FIND_RULES" <<'XML'
<?xml version="1.0"?>
<!-- Written by scripts/retrodeck-eden.sh: see the find rule step there. -->
<ruleList>
    <emulator name="EDEN">
        <rule type="staticpath">
            <entry>/var/data/retrodeck/external_components/eden/component_launcher.sh</entry>
        </rule>
    </emulator>
</ruleList>
XML
  echo "wrote $CUSTOM_FIND_RULES"
fi

step "Eden external content"
# Point Eden's external content dir at the ROM folder so the update and DLC
# NSPs that multi-file downloads put next to the base apply without installing
# them to NAND. Eden scans it recursively. Eden rewrites its config on exit, so
# it must not be running.
if pgrep -f '[e]xternal_components/eden/bin/eden' >/dev/null || pgrep -f '[E]den.AppImage' >/dev/null; then
  echo "Eden is running, close it and re-run" >&2
  exit 1
fi
python3 - "$HOME/.config/eden/qt-config.ini" "$HOME/retrodeck/roms/switch" <<'PY'
import sys
cfg, roms = sys.argv[1:]
lines = open(cfg, encoding="utf-8").read().splitlines()
key = "Paths\\external_content_dirs\\"
idx = [i for i, l in enumerate(lines) if l.startswith(key)]
if not idx:
    # Builds before external content support never wrote the key; it belongs in
    # [UI] with the other Paths\ entries, so put it after the game dirs.
    anchor = next((i for i, l in enumerate(lines) if l.startswith("Paths\\gamedirs\\size=")), None)
    if anchor is None:
        sys.exit(f"no Paths\\gamedirs in {cfg}; start Eden once so it writes its config")
    idx = [anchor + 1]
dirs = [lines[i].split("=", 1)[1] for i in idx if "\\path=" in lines[i]]
if roms in dirs:
    print("already set")
    sys.exit()
dirs.append(roms)
block = [f"{key}{n}\\path={d}" for n, d in enumerate(dirs, 1)] + [f"{key}size={len(dirs)}"]
lines = lines[:idx[0]] + block + [l for l in lines[idx[0]:] if not l.startswith(key)]
open(cfg, "w", encoding="utf-8").write("\n".join(lines) + "\n")
print("added", roms)
PY

cat <<EOF

Done. Restart RetroDECK; Switch games in $HOME/retrodeck/roms/switch now start in Eden.
EOF
