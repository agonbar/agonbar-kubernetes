#!/usr/bin/env bash
# Install the WoW 3.3.5a client from GameVault (the WoW Aura copy, realmlist
# wow.wowaura.com) on the Bazzite VM, launched through Faugus like Battle.net.
# Run this FROM work-vm-00: nas02 is only reachable over the tailnet and the
# Bazzite box is not on it.
#
#   bazzite-wow335a.sh copy      pull the 7z from nas02 into the VM, verified per chunk
#   bazzite-wow335a.sh extract   unpack it into ~/Games and drop the archive
#   bazzite-wow335a.sh setup     Faugus prefix + game entry, .desktop, Steam shortcut
#   bazzite-wow335a.sh all
#
# Why chunks: a single SSH stream from nas02 tops out around 37 Mbit/s, while six
# in parallel fill proxmox-agb00's 100 Mbit NIC (~98), so the copy runs PARALLEL
# streams of CHUNK_MB each. A chunk only counts as done once md5 matches on both
# ends, and done chunks are remembered in STATE, so re-running `copy` resumes
# instead of starting over.
set -euo pipefail

NAS=(ssh -o BatchMode=yes -i "$HOME/.ssh/nas" truenas_admin@100.72.0.41)
VM=(ssh -o BatchMode=yes bazzite@192.168.1.207)
SRC="/mnt/RAID/docker/gamevault-library/World of Warcraft [Wrath of the Lich King] (v3.3.5a) (2008).7z"
ARCHIVE='/home/bazzite/Games/.wow-335a.7z'
GAME_DIR='/home/bazzite/Games/wow-335a'
PREFIX='/home/bazzite/Faugus/wow335a'
GAMEID=wow335a
TITLE='WoW 3.3.5a (Aura)'
CHUNK_MB=${CHUNK_MB:-512}
PARALLEL=${PARALLEL:-6}
STATE="$HOME/.cache/bazzite-wow335a"

step() { printf '\n== %s\n' "$*"; }

chunk() {
  local i=$1 skip=$(( $1 * CHUNK_MB )) a b
  [[ -e "$STATE/$i.ok" ]] && return 0
  "${NAS[@]}" "dd if='$SRC' bs=1M skip=$skip count=$CHUNK_MB status=none" |
    "${VM[@]}" "dd of='$ARCHIVE' bs=1M seek=$skip conv=notrunc iflag=fullblock status=none"
  a=$("${NAS[@]}" "dd if='$SRC' bs=1M skip=$skip count=$CHUNK_MB status=none | md5sum")
  b=$("${VM[@]}" "dd if='$ARCHIVE' bs=1M skip=$skip count=$CHUNK_MB status=none | md5sum")
  if [[ "${a%% *}" == "${b%% *}" ]]; then
    touch "$STATE/$i.ok"
    echo "chunk $i ok"
  else
    echo "chunk $i md5 mismatch, re-run copy" >&2
    return 1
  fi
}

cmd_copy() {
  local size n
  mkdir -p "$STATE"
  size=$("${NAS[@]}" "stat -c %s '$SRC'")
  n=$(( (size + CHUNK_MB * 1048576 - 1) / (CHUNK_MB * 1048576) ))
  step "copy $size bytes as $n chunks of ${CHUNK_MB}MiB, $PARALLEL at a time"
  "${VM[@]}" "mkdir -p '${ARCHIVE%/*}'"
  export -f chunk
  export SRC ARCHIVE CHUNK_MB STATE
  export NAS_S="${NAS[*]}" VM_S="${VM[*]}"
  seq 0 $(( n - 1 )) | xargs -P "$PARALLEL" -I{} bash -c \
    'NAS=($NAS_S); VM=($VM_S); chunk {}'
  "${VM[@]}" "truncate -s $size '$ARCHIVE'"
  [[ $(ls "$STATE"/*.ok | wc -l) -eq $n ]]
  echo "all $n chunks verified"
}

cmd_extract() {
  step "extract into $GAME_DIR"
  "${VM[@]}" "bash -s -- $(printf '%q ' "$ARCHIVE" "$GAME_DIR")" <<'EOF'
set -euo pipefail
archive=$1 game=$2 tmp=$2.tmp
if [[ -e "$game/wow.exe" ]]; then echo "already extracted"; exit 0; fi
rm -rf "$tmp"
7z x -y -bso0 -bsp0 -o"$tmp" "$archive" </dev/null
mv "$tmp/World of Warcraft" "$game"
echo "left over at the archive root, discarded:"; ls -A "$tmp"
rm -rf "$tmp" "$archive"
du -sh "$game"
EOF
  # The chunk markers describe an archive that no longer exists.
  rm -rf "$STATE"
}

# The Faugus entry mirrors the Battle.net one, minus WINE_SIMULATE_WRITECOPY
# (a Battle.net launcher workaround). faugus-steam-launch.sh already picks the
# Wayland or X11 driver per session, so the .desktop and the Steam shortcut both
# go through it, and Steam files the script path plus "wow335a" as launch options
# exactly like the two shortcuts that already work.
cmd_setup() {
  step "Faugus entry, desktop file and Steam shortcut"
  "${VM[@]}" "bash -s -- $(printf '%q ' "$GAME_DIR" "$PREFIX" "$GAMEID" "$TITLE")" <<'EOF'
set -euo pipefail
game=$1 prefix=$2 id=$3 title=$4
data=$HOME/.var/app/io.github.Faugus.faugus-launcher/data/faugus-launcher
icon=$data/icons/$id.png
desktop=$HOME/.local/share/applications/$id.desktop
exe=$game/wow.exe

# Spanish client when the esES data is there; the archive also carries enGB.
mkdir -p "$game/WTF" "$prefix"
if [[ ! -e "$game/WTF/Config.wtf" && -d "$game/Data/esES" ]]; then
  printf 'SET locale "esES"\nSET gxWindow "1"\nSET gxMaximize "1"\n' > "$game/WTF/Config.wtf"
fi

if [[ ! -e "$icon" ]]; then
  # Not /tmp: the flatpak sandbox gets a private one.
  t=$(mktemp -d -p "$HOME/.cache")
  flatpak run --command=icoextract io.github.Faugus.faugus-launcher "$exe" "$t/wow.ico" </dev/null
  magick "$t/wow.ico" "$t/f-%d.png"
  best=$(identify -format '%w %i\n' "$t"/f-*.png | sort -n | tail -1 | cut -d' ' -f2)
  cp "$best" "$icon"
  rm -rf "$t"
fi

python3 - "$data/games.json" "$id" "$title" "$exe" "$prefix" "$icon" <<'PY'
import json, shutil, sys, time
path, gid, title, exe, prefix, icon = sys.argv[1:]
games = json.load(open(path))
if any(g["gameid"] == gid for g in games):
    print("Faugus entry already there"); sys.exit()
shutil.copy(path, f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
entry = dict(next(g for g in games if g["gameid"] == "battlenet"))
entry.update(gameid=gid, title=title, path=exe, prefix=prefix, icon=icon,
             launch_arguments="PROTON_ENABLE_WAYLAND=$FAUGUS_PROTON_WAYLAND",
             game_arguments="", addapp_bat=exe.rsplit("/", 1)[0] + f"/faugus-{gid}.bat",
             playtime=0, cover="", steamgriddb_id="")
games.append(entry)
json.dump(games, open(path, "w"), indent=4)
print("Faugus entry added")
PY

cat > "$desktop" <<DESK
[Desktop Entry]
Name=$title
Exec=$HOME/.local/bin/faugus-steam-launch.sh $id
Icon=$icon
Type=Application
Categories=Game;
Path=$game
DESK

vdf=$(ls "$HOME"/.local/share/Steam/userdata/*/config/shortcuts.vdf | head -1)
if strings "$vdf" | grep -qxF "$title"; then
  echo "already in Steam"
elif pgrep -x steam >/dev/null; then
  steamos-add-to-steam "$desktop" </dev/null
  echo "added to Steam, look under Non-Steam"
else
  echo "Steam not running: add it later with steamos-add-to-steam $desktop"
fi
EOF
}

case "${1:-}" in
  copy) cmd_copy ;;
  extract) cmd_extract ;;
  setup) cmd_setup ;;
  all) cmd_copy; cmd_extract; cmd_setup ;;
  *) sed -n '2,12p' "$0"; exit 1 ;;
esac
