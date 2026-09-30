#!/usr/bin/env bash
# Install RetroDECK on a Bazzite or SteamOS host and run the unreleased
# RomM-integration ES-DE fork inside its flatpak sandbox. Run this ON the
# target machine, e.g. from work-vm-00:
#   ssh deck@<steamdeck> bash -s < scripts/retrodeck-romm.sh
#
# Why a fork and not a release: RetroDECK/ES-DE branch feat/romm-integration
# carries the native RomM client (device-flow pairing, per-platform sync,
# download on demand). It is not in any shipped build -- the daily 0.11.0
# cooker flatpaks still bundle es-de "retrodeck-main-20260414-105925". The only
# published artifact of the branch is the AppImage pinned below.
#
# Why this works: RetroDECK's es-de component IS that AppImage extracted
# (same bin/lib/share layout, same 202 bundled libs). So we leave the read-only
# /app alone, keep our own extracted copy, and start it with the environment
# RetroDECK's own launcher uses (--home $XDG_CONFIG_HOME).
#
# Idempotent: re-run after a RetroDECK update or to move to a newer fork build.
set -euo pipefail

FLATPAK_ID=net.retrodeck.retrodeck
ESDE_ROMM_TAG=${ESDE_ROMM_TAG:-feat-romm-integration-20260827-062348}
ESDE_URL="https://github.com/RetroDECK/ES-DE/releases/download/${ESDE_ROMM_TAG}/ES-DE_x64_RetroDECK.AppImage"
ROMM_URL=${ROMM_URL:-https://romm.adriangonzalezbarbosa.eu}

PREFIX="$HOME/.local/share/retrodeck-romm"
RD_CONFIG="$HOME/.var/app/$FLATPAK_ID/config"
RD_CONF_JSON="$RD_CONFIG/retrodeck/retrodeck.json"
RD_LOCK="$RD_CONFIG/retrodeck/.lock"
ES_SETTINGS="$RD_CONFIG/ES-DE/settings/es_settings.xml"

step() { printf '\n== %s\n' "$*"; }

step "RetroDECK flatpak"
if flatpak info --user "$FLATPAK_ID" >/dev/null 2>&1; then
  echo "present: $(flatpak list --user --app --columns=application,version | grep "$FLATPAK_ID")"
else
  # --user matters: 'flathub' exists in both installations on Bazzite, so a
  # bare 'flatpak install' dies with "found in multiple installations".
  flatpak install -y --user --noninteractive flathub "$FLATPAK_ID"
fi

step "first-time setup"
if [[ -f "$RD_LOCK" ]]; then
  echo "already initialised ($RD_LOCK)"
else
  # RetroDECK's finit() is a chain of zenity dialogs, unusable over SSH. The
  # first run writes retrodeck.json with the Steam Deck default /home/deck
  # paths; a second run then blocks on a "data folder not found" file picker.
  # So: create the config, repoint it at $HOME, then replay the non-interactive
  # half of finit() (other_functions.sh) by hand.
  [[ -f "$RD_CONF_JSON" ]] || flatpak run --user "$FLATPAK_ID" --help >/dev/null 2>&1 || true
  cp -n "$RD_CONF_JSON" "$RD_CONF_JSON.orig"
  sed -i -E "s|\"/home/[^/\"]+/retrodeck|\"$HOME/retrodeck|g" "$RD_CONF_JSON"
  mkdir -p "$HOME/retrodeck"
  flatpak run --user --command=bash "$FLATPAK_ID" -c '
    set -e
    source /app/libexec/global.sh
    rd_home_path="$HOME/retrodeck"
    prepare_component reset framework
    source_component_functions internal
    source_component_functions external
    prepare_component reset all
    update_component_presets
    deploy_helper_files
    create_lock
  '
  echo "created $(ls "$HOME/retrodeck/roms" | wc -l) rom folders"
fi

step "ES-DE RomM fork $ESDE_ROMM_TAG"
mkdir -p "$PREFIX"
if [[ -f "$PREFIX/esde/tag" ]] && [[ "$(cat "$PREFIX/esde/tag")" == "$ESDE_ROMM_TAG" ]]; then
  echo "already extracted"
else
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  curl -fL --retry 3 --no-progress-meter -o "$tmp/esde.AppImage" "$ESDE_URL"
  chmod +x "$tmp/esde.AppImage"
  ( cd "$tmp" && ./esde.AppImage --appimage-extract >/dev/null )
  rm -rf "$PREFIX/esde"
  mv "$tmp/squashfs-root" "$PREFIX/esde"
  echo "$ESDE_ROMM_TAG" > "$PREFIX/esde/tag"
  "$PREFIX/esde/usr/bin/es-de" --version || true
fi

step "launchers"
# Inner half runs inside the sandbox and mirrors start_retrodeck() minus the
# call to /app's own es-de binary.
cat > "$PREFIX/run-inner.sh" <<INNER
#!/bin/bash
source /app/libexec/global.sh
get_steam_user
splash_screen
prepare_component "startup" "all"
export LD_LIBRARY_PATH="$PREFIX/esde/usr/lib:\${LD_LIBRARY_PATH:-}"
log i "Starting ES-DE RomM fork from $PREFIX"
exec "$PREFIX/esde/usr/bin/es-de" --home "\$XDG_CONFIG_HOME" "\$@"
INNER
chmod +x "$PREFIX/run-inner.sh"

mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/retrodeck-romm" <<OUTER
#!/bin/bash
# RetroDECK with the RomM-integration ES-DE fork. Stock RetroDECK still starts
# normally with: flatpak run --user $FLATPAK_ID
exec flatpak run --user --command=bash $FLATPAK_ID "$PREFIX/run-inner.sh" "\$@"
OUTER
chmod +x "$HOME/.local/bin/retrodeck-romm"

mkdir -p "$HOME/.local/share/applications"
cat > "$HOME/.local/share/applications/retrodeck-romm.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=RetroDECK (RomM fork)
Exec=$HOME/.local/bin/retrodeck-romm
Icon=$FLATPAK_ID
Categories=Game;
DESKTOP

step "Steam shortcut"
# Game Mode only lists what is in shortcuts.vdf, so the .desktop entry alone is
# invisible there. steamos-add-to-steam hands the file to the running Steam via
# steam://addnonsteamgame, which makes Steam do the write itself -- editing
# shortcuts.vdf by hand loses the change when Steam exits.
shortcuts_vdf=$(ls "$HOME"/.steam/steam/userdata/*/config/shortcuts.vdf 2>/dev/null | head -1 || true)
if [[ -n "$shortcuts_vdf" ]] && strings "$shortcuts_vdf" | grep -qF "$HOME/.local/bin/retrodeck-romm"; then
  echo "already in Steam"
elif command -v steamos-add-to-steam >/dev/null && pgrep -x steam >/dev/null; then
  steamos-add-to-steam "$HOME/.local/share/applications/retrodeck-romm.desktop"
  echo "added, look under Non-Steam"
else
  echo "skipped: start Steam, then run 'steamos-add-to-steam ~/.local/share/applications/retrodeck-romm.desktop'"
fi

step "RomM server URL"
if grep -q 'name="RomMServerURL"' "$ES_SETTINGS" 2>/dev/null; then
  grep 'name="RomMServerURL"' "$ES_SETTINGS"
else
  # ES-DE's settings file is a flat list of elements with no root, so append.
  printf '<string name="RomMServerURL" value="%s" />\n' "$ROMM_URL" >> "$ES_SETTINGS"
  echo "set to $ROMM_URL"
fi

cat <<EOF

Done. Start it with:  ~/.local/bin/retrodeck-romm
Then: Main menu -> ROMM INTEGRATION -> pair (approve the code in the RomM web UI).
EOF
