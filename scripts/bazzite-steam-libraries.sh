#!/usr/bin/env bash
# Keep Steam libraries on NTFS disks across reboots on Bazzite. Run ON the
# Bazzite host, with the NTFS UUIDs of the partitions holding the libraries:
#   ssh bazzite@<host> bash -s -- <UUID>... < scripts/bazzite-steam-libraries.sh
#
# Bazzite's own automount skips NTFS, so udisks mounts these disks only when
# the user session starts, racing Steam. When Steam wins it finds the library
# paths missing and rewrites libraryfolders.vdf without them, for good.
#
# 1. fstab: mount each partition at boot, at the same path udisks used
#    (/run/media/$USER/<UUID>) and with the same lowntfs-3g options, ordered
#    before sddm so Steam never starts without them. nofail + a short device
#    timeout keep a missing disk from blocking boot.
# 2. libraryfolders.vdf: add every "<mount>/Program Files (x86)/Steam" that is
#    missing, with its apps read from the appmanifests. Steam must be stopped,
#    or it overwrites the file from memory on exit.
#
# Idempotent: re-run whenever Steam drops the libraries again.
set -euo pipefail

(( $# )) || { echo "usage: $0 <ntfs-uuid>..." >&2; exit 2; }

USER_NAME=${USER_NAME:-$(id -un)}
USER_UID=$(id -u "$USER_NAME")
USER_GID=$(id -g "$USER_NAME")
STEAM=${STEAM:-$HOME/.local/share/Steam}
OPTS="rw,nosuid,nodev,noatime,lazytime,uid=$USER_UID,gid=$USER_GID,umask=0022,big_writes,windows_names,nofail,x-systemd.device-timeout=15s,x-systemd.before=sddm.service"

step() { printf '\n== %s\n' "$*"; }

step "fstab"
for uuid in "$@"; do
  mnt="/run/media/$USER_NAME/$uuid"
  if grep -q "^UUID=$uuid " /etc/fstab; then
    echo "$uuid already in fstab"
  else
    [[ -f /etc/fstab.bak-steam-libraries ]] || sudo cp -a /etc/fstab /etc/fstab.bak-steam-libraries
    echo "UUID=$uuid $mnt lowntfs-3g $OPTS 0 0" | sudo tee -a /etc/fstab
  fi
done
sudo systemctl daemon-reload

for uuid in "$@"; do
  mnt="/run/media/$USER_NAME/$uuid"
  unit=$(systemd-escape -p --suffix=mount "$mnt")
  # systemd makes a .mount unit for udisks mounts too, so is-active can't tell
  # them apart; the udisks one runs its helper with uhelper=udisks2.
  if pgrep -f "$mnt -o .*uhelper=udisks2" >/dev/null; then
    sudo umount "$mnt"
  fi
  sudo systemctl start "$unit"
  echo "$mnt mounted by $unit"
done

step "libraryfolders.vdf"
if pgrep -u "$USER_NAME" -x steam >/dev/null; then
  echo "Steam is running; stop it and re-run to restore the libraries" >&2
  exit 1
fi

vdf="$STEAM/steamapps/libraryfolders.vdf"
missing=()
for uuid in "$@"; do
  lib="/run/media/$USER_NAME/$uuid/Program Files (x86)/Steam"
  [[ -f "$lib/libraryfolder.vdf" ]] || { echo "no Steam library at $lib" >&2; exit 1; }
  if grep -qF "\"path\"		\"$lib\"" "$vdf"; then
    echo "already listed: $lib"
  else
    missing+=("$lib")
  fi
done
(( ${#missing[@]} )) || exit 0

cp -a "$vdf" "$vdf.bak-$(date +%Y%m%d-%H%M%S)"
next=$(grep -cP '^\t"\d+"$' "$vdf")
{
  sed '$d' "$vdf"   # drop the closing brace of "libraryfolders"
  for lib in "${missing[@]}"; do
    contentid=$(sed -n 's/.*"contentid"[[:space:]]*"\([0-9]*\)".*/\1/p' "$lib/libraryfolder.vdf")
    printf '\t"%d"\n\t{\n' "$next"
    printf '\t\t"path"\t\t"%s"\n\t\t"label"\t\t""\n\t\t"contentid"\t\t"%s"\n\t\t"totalsize"\t\t"0"\n' "$lib" "$contentid"
    printf '\t\t"apps"\n\t\t{\n'
    for acf in "$lib"/steamapps/appmanifest_*.acf; do
      appid=$(sed -n 's/^[[:space:]]*"appid"[[:space:]]*"\([0-9]*\)".*/\1/p' "$acf" | head -1)
      size=$(sed -n 's/^[[:space:]]*"SizeOnDisk"[[:space:]]*"\([0-9]*\)".*/\1/p' "$acf" | head -1)
      printf '\t\t\t"%s"\t\t"%s"\n' "$appid" "${size:-0}"
    done
    printf '\t\t}\n\t}\n'
    echo "added: $lib" >&2
    next=$((next + 1))
  done
  echo '}'
} > "$vdf.new"
mv "$vdf.new" "$vdf"
# Steam keeps a second copy under config/; give it the same content.
cp -a "$vdf" "$STEAM/config/libraryfolders.vdf"
