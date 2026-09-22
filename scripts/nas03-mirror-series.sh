#!/usr/bin/env bash
# Move directories out of nas02's RAID/docker dataset to the same path on nas03,
# as a mirror: dataset RAID/docker with nas02's properties, same numeric owners,
# modes and mtimes, no SMB share. Written 2026-09-22 for the two series that
# finished syncing to Pc-Vanesa through the lamg Syncthing pod.
#
#   ./nas03-mirror-series.sh prepare   # dataset on nas03 + temporary sudo/key grants
#   ./nas03-mirror-series.sh copy      # setsid'd rsync on nas02 -> nas03 over the LAN
#   ./nas03-mirror-series.sh status
#   ./nas03-mirror-series.sh verify    # type/mode/owner/mtime/size of every entry
#   ./nas03-mirror-series.sh revoke    # drop the temporary grants
#   NAS02_SUDO_PW=... ./nas03-mirror-series.sh purge   # delete the items on nas02
#
# copy is resumable: rerun it. The Syncthing markers (.stfolder, .stignore) stay
# behind, since the folders leave Syncthing as part of the move. Remove them from
# Syncthing BEFORE deleting anything on nas02: a Send Only folder whose files
# vanish sends the deletes to every device it is shared with. purge refuses to
# run while Syncthing still shares an item or while verify finds a difference.
set -euo pipefail

ITEMS=("media/SERIES/Charmed" "media/SERIES/The Mentalist")
ROOT=/mnt/RAID/docker
NAS03_LAN=192.168.0.34   # rsync endpoint: nas02 and nas03 share the LAN
# Control plane from this box. Defaults work off the home LAN (Tailscale/NetBird).
N02=(ssh -o BatchMode=yes -o ConnectTimeout=10 -i "$HOME/.ssh/nas" truenas_admin@"${N02_HOST:-100.72.0.41}")
N03=(ssh -o BatchMode=yes -o ConnectTimeout=10 "${N03_HOST:-nas03}")
N03_UID=33   # truenas_admin's middleware id on nas03
RSH="ssh -i ~/.ssh/nas03_seed -o BatchMode=yes -o Ciphers=aes128-gcm@openssh.com -o ServerAliveInterval=30"

log(){ echo "[$(date +%H:%M:%S)] $*"; }
die(){ echo "FATAL: $*" >&2; exit 1; }

# rsync filter: the dataset root and the parents are copied too, so their owner
# and mode match nas02's (1000:3000 777 and 1000:1000 777).
filter() {
  printf -- '- .stfolder\n- .stignore\n- .rsync-partial\n'
  local seen="" p
  for it in "${ITEMS[@]}"; do
    p=""
    for part in $(dirname "$it" | tr '/' ' '); do
      p+="/$part"
      [[ " $seen " == *" $p "* ]] || { printf '+ %s/\n' "$p"; seen+=" $p"; }
    done
    printf '+ /%s/***\n' "$it"
  done
  printf -- '- *\n'
}

prepare() {
  if "${N03[@]}" "midclt call pool.dataset.query '[[\"id\",\"=\",\"RAID/docker\"]]'" | grep -q '"id"'; then
    log "nas03: RAID/docker already exists"
  else
    "${N03[@]}" "midclt call pool.dataset.create '{\"name\": \"RAID/docker\", \"share_type\": \"GENERIC\",
      \"acltype\": \"POSIX\", \"aclmode\": \"DISCARD\", \"compression\": \"LZ4\", \"recordsize\": \"1M\", \"atime\": \"OFF\"}'" >/dev/null
    log "nas03: created RAID/docker"
  fi
  "${N03[@]}" "/sbin/zfs get -H -o property,value compression,recordsize,atime,acltype,aclmode,aclinherit,xattr RAID/docker" | tr '\t' '=' | paste -sd' '
  local key; key=$("${N02[@]}" "cat ~/.ssh/nas03_seed.pub")
  "${N03[@]}" "python3 - $N03_UID $(printf %q "$key")" <<'EOF'
import json, subprocess, sys
uid, key = int(sys.argv[1]), sys.argv[2]
u = json.loads(subprocess.check_output(["midclt", "call", "user.query", json.dumps([["id", "=", uid]])]))[0]
keys = [k for k in (u["sshpubkey"] or "").splitlines() if k.strip()]
if key not in keys:
    keys.append(key)
subprocess.check_call(["midclt", "call", "user.update", str(uid), json.dumps(
    {"sshpubkey": "\n".join(keys), "sudo_commands_nopasswd": ["/usr/bin/rsync"]})], stdout=subprocess.DEVNULL)
print(f"nas03: {len(keys)} authorized keys, NOPASSWD rsync granted")
EOF
  "${N02[@]}" "$RSH truenas_admin@$NAS03_LAN 'sudo -n rsync --version | head -1'" \
    || die "nas02 cannot run sudo rsync on nas03"
}

copy() {
  "${N02[@]}" "mkdir -p ~/nas03-mirror && cat > ~/nas03-mirror/filter" < <(filter)
  "${N02[@]}" "cd ~/nas03-mirror && setsid nohup rsync -aHAX --numeric-ids --partial-dir=.rsync-partial \
    --info=stats2,progress2 --filter='merge filter' -e $(printf %q "$RSH") --rsync-path='sudo rsync' \
    $ROOT/ truenas_admin@$NAS03_LAN:$ROOT/ > copy.log 2>&1 < /dev/null &"
  log "started rsync on nas02; log ~/nas03-mirror/copy.log"
}

status() {
  # [m] keeps pgrep from matching the remote shell running this very command.
  "${N02[@]}" "pgrep -f '[m]erge filter' >/dev/null && echo running || echo not-running; tail -c 600 ~/nas03-mirror/copy.log | tr '\r' '\n' | grep -v '^$' | tail -3"
  "${N03[@]}" "/sbin/zfs list -H -o name,used,avail RAID/docker"
}

listing() {
  local q; q=$(printf ' %q' "${ITEMS[@]}")
  # Directory sizes are entry counts on ZFS and the markers are left behind, so
  # only regular files carry a size.
  echo "cd $ROOT && { stat -c '%F %a %u:%g %n' . media media/SERIES;
    find $q \\( -name .stfolder -o -name .stignore \\) -prune -o -printf '%y %m %U:%G %Ts %s %p\\n' \
    | awk '\$1!=\"f\"{\$5=0} 1' | LC_ALL=C sort; }"
}

verify() {
  local a b; a=$(mktemp) b=$(mktemp)
  "${N02[@]}" "$(listing)" > "$a"
  "${N03[@]}" "$(listing)" > "$b"
  if diff "$a" "$b" > /dev/null; then
    log "identical: $(grep -c '^f' "$a") files, $(awk '$1=="f"{s+=$5} END {printf "%.1f GB", s/1e9}' "$a")"
  else
    diff "$a" "$b" | head -20 || true; rm -f "$a" "$b"; die "nas02 and nas03 differ"
  fi
  rm -f "$a" "$b"
}

revoke() {
  local key; key=$("${N02[@]}" "cat ~/.ssh/nas03_seed.pub")
  "${N03[@]}" "python3 - $N03_UID $(printf %q "$key")" <<'EOF'
import json, subprocess, sys
uid, key = int(sys.argv[1]), sys.argv[2]
u = json.loads(subprocess.check_output(["midclt", "call", "user.query", json.dumps([["id", "=", uid]])]))[0]
keys = [k for k in (u["sshpubkey"] or "").splitlines() if k.strip() and k != key]
subprocess.check_call(["midclt", "call", "user.update", str(uid), json.dumps(
    {"sshpubkey": "\n".join(keys), "sudo_commands_nopasswd": []})], stdout=subprocess.DEVNULL)
print(f"nas03: {len(keys)} authorized keys, NOPASSWD rsync revoked")
EOF
}

purge() {
  # The one step with no way back: RAID/docker on nas02 has no snapshots.
  : "${NAS02_SUDO_PW:?set NAS02_SUDO_PW (vault: projects/plex-nas02)}"
  local cfg; cfg=$(kubectl --context lamg -n lamg exec deploy/syncthing -- cat /config/config.xml)
  for it in "${ITEMS[@]}"; do
    ! grep -qF "path=\"/$it\"" <<<"$cfg" || die "Syncthing still shares /$it; remove that folder first"
  done
  verify
  "${N02[@]}" "/sbin/zfs list -H -o name,used,avail RAID/docker"
  printf '%s\n' "$NAS02_SUDO_PW" | "${N02[@]}" "sudo -S -p '' rm -rf --$(printf " $ROOT/%q" "${ITEMS[@]}")"
  "${N02[@]}" "ls -d$(printf " $ROOT/%q" "${ITEMS[@]}") 2>/dev/null" && die "still present on nas02"
  log "deleted on nas02; ZFS frees the space in the background"
  "${N02[@]}" "sleep 30; /sbin/zfs list -H -o name,used,avail RAID/docker"
}

case ${1:-} in
  prepare) prepare ;;
  copy) copy ;;
  status) status ;;
  verify) verify ;;
  revoke) revoke ;;
  purge) purge ;;
  *) sed -n '2,18p' "$0"; exit 1 ;;
esac
