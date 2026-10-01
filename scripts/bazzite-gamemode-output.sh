#!/usr/bin/env bash
# Pick which physical screen Bazzite's Game Mode drives. Run this ON the Bazzite
# machine. Install it as ~/.local/bin/gamemode-output.
#
# There is no mirroring to be had in Game Mode: gamescope's DRM backend scans
# out to a single connector, --prefer-output is a priority list rather than a
# clone switch, and neither `gamescope --help` nor the 100 `gamescopectl help`
# convars (3.16.28) expose a mirror/clone or a runtime connector change. So the
# screen can only be picked at session start, and switching means restarting the
# session. Mirroring both screens at once needs the KDE desktop session, whose
# display settings have a Replicate option, with Big Picture running inside it.
#
# Switching works by setting OUTPUT_CONNECTOR in the systemd user manager and
# restarting the session, which is what the Output Manager Decky plugin does too
# (see install-plugin below), so the two never fight. Deliberately NOT written to
# ~/.config/environment.d: the session sources that with `set -a` after inheriting
# the manager environment, so a file there would silently override every later
# switch. The setting is therefore lost on reboot; for a persistent default use
# the plugin's own Default display setting.
#
#   gamemode-output                          list connected outputs and the current setting
#   gamemode-output <connector> [--restart]  pin Game Mode to that output
#   gamemode-output next [--restart]         cycle to the next connected output
#   gamemode-output install-shortcut         add a "Switch screen" non-Steam shortcut
#   gamemode-output install-plugin           install the Output Manager Decky plugin
set -euo pipefail

SESSION_UNIT="gamescope-session-plus@ogui-steam.service"
STALE_ENV_FILE="$HOME/.config/environment.d/10-gamescope-output.conf"
WRAPPER="$HOME/.local/bin/gamemode-switch-screen"
DESKTOP="$HOME/.local/share/applications/gamemode-switch-screen.desktop"
PLUGIN_URL="https://github.com/joeatethebeans/OutputManager/releases/download/v1.0.0/Output.Manager-1.0.0.zip"

connected() {
  local c name
  for c in /sys/class/drm/card*-*; do
    [[ "$(cat "$c/status" 2>/dev/null)" == connected ]] || continue
    name="${c##*/}"
    echo "${name#card*-}"
  done
}

label_of() {
  local c
  c="$(echo /sys/class/drm/card*-"$1")"
  edid-decode "$c/edid" 2>/dev/null | sed -n "s/.*Display Product Name: '\(.*\)'/\1/p" | head -1
}

in_use() {
  local c name
  for c in /sys/class/drm/card*-*; do
    if [[ "$(cat "$c/enabled" 2>/dev/null)" == enabled ]]; then
      name="${c##*/}"
      echo "${name#card*-}"
      return
    fi
  done
}

configured() {
  systemctl --user show-environment 2>/dev/null | sed -n 's/^OUTPUT_CONNECTOR=//p'
}

restart_session() {
  echo "restarting Game Mode, the screen goes black for a few seconds"
  systemctl --user restart "$SESSION_UNIT"
}

set_output() {
  if [[ -f "$STALE_ENV_FILE" ]]; then
    rm -f "$STALE_ENV_FILE"
    echo "removed $STALE_ENV_FILE, it would have overridden this"
  fi
  systemctl --user set-environment "OUTPUT_CONNECTOR=$1"
  echo "OUTPUT_CONNECTOR=$1 ($(label_of "$1"))"
}

target="${1:-}"

case "$target" in
  "")
    echo "Connected outputs:"
    for o in $(connected); do
      printf '  %-12s %s%s\n' "$o" "$(label_of "$o")" \
        "$([[ "$o" == "$(in_use)" ]] && echo '  <- in use')"
    done
    echo
    cfg="$(configured)"
    echo "Configured: ${cfg:-none (gamescope default *,eDP-1)}"
    exit 0
    ;;

  install-plugin)
    # Output Manager is not in the Decky store and cannot be: it depends on
    # OUTPUT_CONNECTOR, which vanilla SteamOS does not expose. Installing it by
    # hand is the same thing Decky's own "install from ZIP" does.
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    curl -fL --no-progress-meter -o "$tmp/plugin.zip" "$PLUGIN_URL"
    sudo python3 -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" \
      "$tmp/plugin.zip" "$HOME/homebrew/plugins"
    sudo systemctl restart plugin_loader
    echo "installed, open the Decky menu in Game Mode: Output Manager"
    exit 0
    ;;

  install-shortcut)
    mkdir -p "$(dirname "$WRAPPER")" "$(dirname "$DESKTOP")"
    printf '#!/bin/bash\nexec %s next --restart\n' "$(realpath "$0")" > "$WRAPPER"
    chmod +x "$WRAPPER"
    cat > "$DESKTOP" <<DESKTOP_ENTRY
[Desktop Entry]
Type=Application
Name=Switch screen (Game Mode)
Exec=$WRAPPER
Icon=preferences-desktop-display
Categories=Settings;
DESKTOP_ENTRY
    vdf=$(ls "$HOME"/.steam/steam/userdata/*/config/shortcuts.vdf 2>/dev/null | head -1 || true)
    if [[ -n "$vdf" ]] && strings "$vdf" | grep -qF "$WRAPPER"; then
      echo "already in Steam"
    elif command -v steamos-add-to-steam >/dev/null && pgrep -x steam >/dev/null; then
      steamos-add-to-steam "$DESKTOP"
      echo "added to Steam, look under Non-Steam"
    else
      echo "Steam not running: add it later with steamos-add-to-steam $DESKTOP"
    fi
    exit 0
    ;;

  next)
    mapfile -t outs < <(connected)
    if [[ "${#outs[@]}" -lt 2 ]]; then
      echo "only one output connected, nothing to switch to" >&2
      exit 1
    fi
    current="$(configured)"
    [[ -n "$current" ]] || current="$(in_use)"
    next="${outs[0]}"
    for i in "${!outs[@]}"; do
      [[ "${outs[$i]}" == "$current" ]] && next="${outs[$(( (i + 1) % ${#outs[@]} ))]}"
    done
    set_output "$next"
    [[ "${2:-}" == "--restart" ]] && restart_session
    exit 0
    ;;
esac

if ! compgen -G "/sys/class/drm/card*-$target" > /dev/null; then
  echo "No such connector: $target" >&2
  connected >&2
  exit 1
fi

set_output "$target"
if [[ "${2:-}" == "--restart" ]]; then
  restart_session
else
  echo "takes effect on the next Game Mode start: log out from the Steam UI,"
  echo "or run: systemctl --user restart $SESSION_UNIT"
fi
