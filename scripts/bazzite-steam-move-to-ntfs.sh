#!/usr/bin/env bash
# Move an installed Steam game from Bazzite's internal library to one of the
# NTFS libraries. Run ON the Bazzite host, in two steps:
#   ssh bazzite@<host> bash -s -- copy   <appid> <ntfs-uuid> < scripts/bazzite-steam-move-to-ntfs.sh
#   ssh bazzite@<host> bash -s -- switch <appid> <ntfs-uuid> < scripts/bazzite-steam-move-to-ntfs.sh
#
# Steam's own "move install folder" can't do this. steamapps/compatdata on the
# NTFS libraries is a symlink to the internal compatdata, because Proton
# prefixes can't live on NTFS. Steam creates compatdata/<appid>/config_info,
# tries to move it onto itself, cancels with "Can't overwrite existing file"
# after copying the whole game, and its rollback deletes the prefix.
#
# copy:   rsync the game folder to the NTFS library. Steam can keep running.
# switch: stop the session, re-sync, check both trees match, move the
#         appmanifest across, delete the internal copy and start the session.
#         The prefix stays where it is, the symlink already serves it. The
#         shader cache is dropped and Steam fetches it again.
set -euo pipefail

(( $# == 3 )) && [[ $1 == copy || $1 == switch ]] || { echo "usage: $0 copy|switch <appid> <ntfs-uuid>" >&2; exit 2; }
mode=$1 appid=$2 uuid=$3

USER_NAME=${USER_NAME:-$(id -un)}
STEAM=${STEAM:-$HOME/.local/share/Steam}
src_lib="$STEAM/steamapps"
dst_lib="/run/media/$USER_NAME/$uuid/Program Files (x86)/Steam/steamapps"
manifest="$src_lib/appmanifest_$appid.acf"

[[ -f $manifest ]] || { echo "app $appid is not installed in $src_lib" >&2; exit 1; }
[[ -f "$dst_lib/../libraryfolder.vdf" ]] || { echo "no Steam library at $dst_lib" >&2; exit 1; }
findmnt -no OPTIONS "/run/media/$USER_NAME/$uuid" | grep -q '^rw,' || { echo "$uuid is not mounted read-write" >&2; exit 1; }

installdir=$(sed -n 's/^[[:space:]]*"installdir"[[:space:]]*"\(.*\)"$/\1/p' "$manifest")
src="$src_lib/common/$installdir"
dst="$dst_lib/common/$installdir"
[[ -n $installdir && -d $src ]] || { echo "no game folder at $src" >&2; exit 1; }

# NTFS keeps 100 ns timestamps, so compare mtimes with a one second window or
# every file looks changed.
sync_game() { rsync -rt --delete --modify-window=1 "$@" "$src/" "$dst/"; }
steam_running() { pgrep -u "$USER_NAME" -x steam >/dev/null; }

if [[ $mode == copy ]]; then
  need=$(du -sk "$src" | cut -f1)
  avail=$(df -k --output=avail "$dst_lib" | tail -1)
  (( avail > need + 1048576 )) || { echo "not enough space on $uuid: need $need KiB, have $avail" >&2; exit 1; }
  mkdir -p "$dst"
  sync_game --info=stats1
  echo "copied to $dst"
  exit 0
fi

[[ -d $dst ]] || { echo "run copy first" >&2; exit 1; }
if pgrep -f 'SteamLaunch AppId=' >/dev/null; then
  echo "a game is running, not stopping the session" >&2
  exit 1
fi

# Never start sddm with Steam still alive: the game session then fails on every
# start until gamescope-session-plus untars its Steam bootstrap over the install.
start_session() {
  if steam_running; then
    echo "Steam is still running, sddm left stopped" >&2
  else
    sudo systemctl start sddm
  fi
}
trap start_session EXIT

sudo systemctl stop sddm
systemctl --user stop app-steam@autostart.service 2>/dev/null || true
for _ in $(seq 60); do steam_running || break; sleep 1; done
steam_running && { echo "Steam did not stop" >&2; exit 1; }

sync_game
diff=$(sync_game --dry-run --itemize-changes)
[[ -z $diff ]] || { echo "copy does not match the source, nothing deleted:" >&2; echo "$diff" | head -20 >&2; exit 1; }

cp "$manifest" "$dst_lib/"
rm "$manifest"
rm -rf "$src" "$src_lib/shadercache/$appid"
echo "app $appid now lives in $dst"
