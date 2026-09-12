#!/usr/bin/env bash
# Copy nas00's Public (degraded mdadm RAID5, mounted ro at /mnt/raid-ro) to nas03's
# RAID/Public, reading as little as possible from nas00.
#
# About 4.8T of nas00's Public already sits on nas02 with the same path, size and
# mtime. That part is seeded from nas02's healthy ZFS pool; only the rest (~2.9T,
# the data that exists nowhere else) is read off the degraded array.
#
#   ./nas03-seed-public.sh plan         # list both sides, split into the two file lists
#   ./nas03-seed-public.sh copy-nas00   # nas00-only files, one top-level dir at a time
#   ./nas03-seed-public.sh seed-nas02   # files identical on nas02
#   ./nas03-seed-public.sh status       # progress + md/disk errors on nas00
#   ./nas03-seed-public.sh verify       # dry-run nas00 -> nas03, must list no files
#
# copy-nas00 runs as a transient systemd unit on nas00 and seed-nas02 as a setsid'd
# rsync on nas02, so both survive this laptop disconnecting. Both are resumable:
# rerun the same subcommand. Rerun `plan` first if nas02's Public changed.
#
# Prereqs done 2026-09-12: nas03 truenas_admin has NOPASSWD sudo for /usr/bin/rsync
# (remove it when finished) and authorizes ~/.ssh/nas03_seed from nas00 (user lamg)
# and nas02 (truenas_admin). Files land as uid/gid 3000 (lamg), like nas02's Public.
set -euo pipefail

NAS03=truenas_admin@192.168.0.34
DST=/mnt/RAID/Public/
N00_SRC=/mnt/raid-ro/Public
N02_SRC=/mnt/RAID/Public
WORK=${WORK:-$HOME/.cache/nas03-seed}
# nas00 first: _MABEL/_LAMG/_GUZMAN/_ADRIAN are personal and small, Shared Pictures last.
ORDER=(_MABEL _LAMG _GUZMAN _ADRIAN "Shared Pictures")
N00=(ssh -F /dev/null -i "$HOME/.ssh/nas" -o BatchMode=yes lamg@192.168.0.24)
N02=(ssh -o BatchMode=yes -o HostName=192.168.0.29 -o HostKeyAlias=nas02 nas02)
RSH="ssh -i ~/.ssh/nas03_seed -o BatchMode=yes -o Ciphers=aes128-gcm@openssh.com -o ServerAliveInterval=30"
RSYNC_OPTS="-a --chown=3000:3000 --partial-dir=.rsync-partial --info=stats2,progress2"

log(){ echo "[$(date +%H:%M:%S)] $*"; }
die(){ echo "FATAL: $*" >&2; exit 1; }
mkdir -p "$WORK"

check_ro_mount() {
  "${N00[@]}" "findmnt -no OPTIONS /mnt/raid-ro | grep -qw ro" \
    || die "nas00:/mnt/raid-ro is not mounted read-only (sudo mount -o ro,noload /dev/md1p1 /mnt/raid-ro)"
}

plan() {
  check_ro_mount
  log "listing nas00 (NUL-separated size/mtime/path)"
  "${N00[@]}" "cd $N00_SRC && sudo -n find . -type f -printf '%s\t%T@\t%P\0'" > "$WORK/nas00.lst"
  log "listing nas02"
  "${N02[@]}" "cd $N02_SRC && find . -type f -printf '%s\t%T@\t%P\0'" > "$WORK/nas02.lst"
  python3 - "$WORK" <<'EOF'
import sys, os, collections
work = sys.argv[1]
def load(p):
    d = {}
    for rec in open(p, "rb").read().split(b"\0"):
        if rec:
            s, t, path = rec.split(b"\t", 2)
            d[path] = (int(s), int(float(t)))
    return d
a, b = load(f"{work}/nas00.lst"), load(f"{work}/nas02.lst")
from02, from00 = [], collections.defaultdict(list)
tot = collections.defaultdict(lambda: [0, 0])
for path, (s, t) in sorted(a.items()):
    top = path.split(b"/")[0] if b"/" in path else b"."
    other = b.get(path)
    if other and other[0] == s and abs(other[1] - t) <= 2:
        from02.append(path); tot["nas02"][0] += 1; tot["nas02"][1] += s
    else:
        from00[top].append(path); tot["nas00"][0] += 1; tot["nas00"][1] += s
open(f"{work}/from-nas02.lst", "wb").write(b"\0".join(from02) + b"\0")
for f in os.listdir(work):
    if f.startswith("from-nas00."): os.remove(f"{work}/{f}")
for top, paths in from00.items():
    open(f"{work}/from-nas00.{top.decode(errors='replace')}.lst", "wb").write(b"\0".join(paths) + b"\0")
    print(f"  nas00 {top.decode(errors='replace'):20} {len(paths):>7} files {sum(a[p][0] for p in paths)/1024**3:>7.0f} G")
for k, (n, s) in tot.items():
    print(f"from {k}: {n} files, {s/1024**3:.0f} G")
EOF
  log "uploading lists"
  "${N00[@]}" "mkdir -p ~/nas03-seed && rm -f ~/nas03-seed/from-nas00.*"
  for f in "$WORK"/from-nas00.*.lst; do "${N00[@]}" "cat > ~/nas03-seed/$(printf %q "$(basename "$f")")" < "$f"; done
  "${N02[@]}" "mkdir -p ~/nas03-seed && cat > ~/nas03-seed/from-nas02.lst" < "$WORK/from-nas02.lst"
}

copy_nas00() {
  check_ro_mount
  # Ordered dirs first, then every list again to catch leftovers (loose files); rsync
  # skips what is already there. The final dirs-only pass creates empty directories,
  # which file lists never carry, and fixes directory mtimes after the writes.
  local cmd="set -e" rs="rsync $RSYNC_OPTS -e '${RSH//\~//home/lamg}' --rsync-path='sudo rsync'"
  for d in "${ORDER[@]}"; do
    local lst="/home/lamg/nas03-seed/from-nas00.$d.lst"
    cmd+="; if [ -f '$lst' ]; then echo \"== \$(date -Is) $d\"; $rs --from0 --files-from='$lst' $N00_SRC/ $NAS03:$DST; fi"
  done
  cmd+="; for lst in /home/lamg/nas03-seed/from-nas00.*.lst; do echo \"== \$(date -Is) \$lst\"; $rs --from0 --files-from=\"\$lst\" $N00_SRC/ $NAS03:$DST; done"
  cmd+="; echo \"== \$(date -Is) dirs\"; $rs -f'+ */' -f'- *' $N00_SRC/ $NAS03:$DST"
  cmd+="; echo \"== \$(date -Is) done\""
  "${N00[@]}" "sudo -n systemd-run --unit=nas03-seed-nas00 --property=Nice=10 --property=IOSchedulingClass=idle \
    bash -c $(printf %q "{ $cmd; } > /var/log/nas03-seed-nas00.log 2>&1")"
  log "started nas03-seed-nas00 on nas00; log /var/log/nas03-seed-nas00.log"
}

seed_nas02() {
  # BWLIMIT (KiB/s) leaves room on nas03's 1GbE when copy-nas00 runs at the same time.
  "${N02[@]}" "cd ~/nas03-seed && setsid nohup rsync $RSYNC_OPTS ${BWLIMIT:+--bwlimit=$BWLIMIT} --from0 --files-from=from-nas02.lst \
    -e $(printf %q "$RSH") --rsync-path='sudo rsync' $N02_SRC/ $NAS03:$DST > seed-nas02.log 2>&1 < /dev/null &"
  log "started rsync on nas02; log ~/nas03-seed/seed-nas02.log"
}

status() {
  echo "== nas00 unit"; "${N00[@]}" "systemctl is-active nas03-seed-nas00 2>/dev/null; sudo -n grep '^==' /var/log/nas03-seed-nas00.log 2>/dev/null | tail -3; sudo -n tail -c 400 /var/log/nas03-seed-nas00.log 2>/dev/null | tr '\r' '\n' | tail -2" || true
  echo "== nas00 md/disk errors since boot"; "${N00[@]}" "cat /proc/mdstat | grep -A1 ^md1; sudo -n dmesg | grep -i -E 'md/raid.*(fail|error|disabl)|I/O error|medium error|ata[0-9].*(error|reset)' | tail -5" || true
  # [f] keeps pgrep from matching the remote shell running this very command.
  echo "== nas02 seed"; "${N02[@]}" "pgrep -f '[f]iles-from=from-nas02' >/dev/null && echo running || echo not-running; tail -c 400 ~/nas03-seed/seed-nas02.log 2>/dev/null | tr '\r' '\n' | tail -2" || true
  echo "== nas03"; ssh -i "$HOME/.ssh/nas" -o BatchMode=yes "$NAS03" "zfs list -H -o used,avail RAID/Public 2>/dev/null || df -h $DST | tail -1"
}

verify() {
  check_ro_mount
  "${N00[@]}" "sudo -n rsync -a -n -i --chown=3000:3000 --exclude=.rsync-partial -e '${RSH//\~//home/lamg}' --rsync-path='sudo rsync' $N00_SRC/ $NAS03:$DST" \
    | grep -v '^\.d' || log "nothing to transfer: nas03 has every nas00 file"
}

case ${1:-} in
  plan) plan ;;
  copy-nas00) copy_nas00 ;;
  seed-nas02) seed_nas02 ;;
  status) status ;;
  verify) verify ;;
  *) sed -n '2,22p' "$0"; exit 1 ;;
esac
