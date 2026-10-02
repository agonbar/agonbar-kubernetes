#!/usr/bin/env bash
# Nightly backup of nas02's Public onto nas03, now that nas03 lives on the aya LAN.
#
# nas03 pulls over the tailnet as root into its own dataset RAID/mirror/nas02-Public
# (never into nas03's RAID/Public, which holds nas00's rescued content and would be
# wiped by --delete). After every good run it snapshots the dataset and keeps the
# last KEEP snapshots, so a delete or an overwrite on nas02 can be undone from nas03.
# On nas02 the key may only run `rrsync -ro /mnt/RAID/Public`: read-only, one tree.
#
#   NAS03_SUDO_PW=... ./nas03-mirror-public.sh setup   # dataset, key, nas02 grant, seed, cron
#   ./nas03-mirror-public.sh run      # start a sync now (the cron job, run once)
#   ./nas03-mirror-public.sh status   # running?, last log lines, last good run, snapshots
#   ./nas03-mirror-public.sh verify   # dry-run from nas02, must list no changes
#
# setup is idempotent. The seed block-clones nas03's RAID/Public into the mirror
# (block cloning, no extra space), so the first sync only fetches what nas02 has
# that nas03 lacks instead of the whole 8T across the WAN.
set -euo pipefail

N02_TS=100.72.0.41      # nas02 on the tailnet, as seen from nas03 (no mesh DNS there)
DS=RAID/mirror/nas02-Public
BASE=/mnt/RAID/mirror   # parent dataset: key, known_hosts, sync script, log
KEEP=${KEEP:-30}
SCHEDULE='{"minute": "0", "hour": "2", "dom": "*", "month": "*", "dow": "*"}'
N02=(ssh -o BatchMode=yes -o ConnectTimeout=10 -i "$HOME/.ssh/nas" truenas_admin@"${N02_HOST:-$N02_TS}")
N03=(ssh -o BatchMode=yes -o ConnectTimeout=10 "${N03_HOST:-nas03}")

log(){ echo "[$(date +%H:%M:%S)] $*"; }
die(){ echo "FATAL: $*" >&2; exit 1; }
sudo03(){ printf '%s\n' "$NAS03_SUDO_PW" | "${N03[@]}" "sudo -S -p '' $1"; }

# Runs on nas03 as root from cron. flock keeps a slow first sync from overlapping
# the next night's run.
sync_script() {
  cat <<EOF
#!/bin/bash
set -uo pipefail
exec 9>$BASE/.lock; flock -n 9 || { echo "already running"; exit 0; }
exec > $BASE/nas02-public.log 2>&1
echo "== \$(date -Is) start"
rsync -aHAX --numeric-ids --delete --partial-dir=.rsync-partial --info=stats2 \\
  -e "ssh -i $BASE/id_ed25519 -o BatchMode=yes -o UserKnownHostsFile=$BASE/known_hosts -o ServerAliveInterval=30" \\
  truenas_admin@$N02_TS:/ /mnt/$DS/
rc=\$?
echo "== \$(date -Is) rsync exit \$rc"
# 24 = files vanished on nas02 mid-run, normal on a live share.
[ \$rc -eq 0 ] || [ \$rc -eq 24 ] || exit \$rc
/sbin/zfs snapshot $DS@mirror-\$(date +%Y%m%d-%H%M)
/sbin/zfs list -H -t snapshot -o name -s creation $DS | grep '@mirror-' | head -n -$KEEP | xargs -r -n1 /sbin/zfs destroy
date -Is > $BASE/last-ok
EOF
}

# Seeds the mirror from nas03's RAID/Public. cp --reflink fails across datasets
# (FICLONE gives EXDEV) and --reflink=auto silently copies, but copy_file_range
# does clone across datasets of one pool. Mode, owner and mtime come along so the
# first rsync skips identical files; it fixes ACLs, xattrs and hardlinks itself.
seed_script() {
  cat <<'EOF'
import os, sys
src, dst = sys.argv[1], sys.argv[2]
def meta(s, d, st):
    os.chown(d, st.st_uid, st.st_gid, follow_symlinks=False)
    if not os.path.islink(d):
        os.chmod(d, st.st_mode & 0o7777)
    os.utime(d, ns=(st.st_atime_ns, st.st_mtime_ns), follow_symlinks=False)
dirs = []
for root, ds, fs in os.walk(src):
    out = os.path.join(dst, os.path.relpath(root, src))
    os.makedirs(out, exist_ok=True)
    dirs.append((root, out))
    for f in fs:
        s, d = os.path.join(root, f), os.path.join(out, f)
        st = os.lstat(s)
        if os.path.islink(s):
            if not os.path.lexists(d):
                os.symlink(os.readlink(s), d)
        else:
            with open(s, 'rb') as a, open(d, 'wb') as b:
                off = 0
                while off < st.st_size:
                    n = os.copy_file_range(a.fileno(), b.fileno(), st.st_size - off)
                    if n == 0:
                        break
                    off += n
        meta(s, d, st)
for s, d in reversed(dirs):
    meta(s, d, os.lstat(s))
print("seeded", len(dirs), "dirs")
EOF
}

setup() {
  : "${NAS03_SUDO_PW:?set NAS03_SUDO_PW (vault: projects/nas03)}"
  for d in RAID/mirror "$DS"; do
    if "${N03[@]}" "midclt call pool.dataset.query '[[\"id\",\"=\",\"$d\"]]'" | grep -q '"id"'; then
      log "nas03: $d already exists"
    else
      "${N03[@]}" "midclt call pool.dataset.create '{\"name\": \"$d\", \"share_type\": \"GENERIC\",
        \"acltype\": \"POSIX\", \"aclmode\": \"DISCARD\", \"compression\": \"LZ4\", \"recordsize\": \"1M\", \"atime\": \"OFF\"}'" >/dev/null
      log "nas03: created $d"
    fi
  done

  sudo03 "bash -c '[ -f $BASE/id_ed25519 ] || ssh-keygen -q -t ed25519 -N \"\" -C nas03-mirror-public -f $BASE/id_ed25519'"
  local pub; pub=$("${N03[@]}" "cat $BASE/id_ed25519.pub")
  "${N02[@]}" "python3 - $(printf %q "$pub")" <<'EOF'
import json, subprocess, sys
key = sys.argv[1]
entry = 'command="/usr/bin/rrsync -ro /mnt/RAID/Public",restrict ' + key
u = json.loads(subprocess.check_output(["midclt", "call", "user.query", json.dumps([["username", "=", "truenas_admin"]])]))[0]
keys = [k for k in (u["sshpubkey"] or "").splitlines() if k.strip() and key not in k]
keys.append(entry)
subprocess.check_call(["midclt", "call", "user.update", str(u["id"]), json.dumps({"sshpubkey": "\n".join(keys)})],
                      stdout=subprocess.DEVNULL)
print(f"nas02: {len(keys)} authorized keys, mirror key restricted to rrsync -ro")
EOF
  sudo03 "bash -c 'ssh-keyscan -t ed25519 $N02_TS > $BASE/known_hosts 2>/dev/null'"
  # sudo -S already owns stdin, so the script goes via /tmp.
  sync_script | "${N03[@]}" "cat > /tmp/nas02-public-sync.sh"
  sudo03 "install -m 700 /tmp/nas02-public-sync.sh $BASE/nas02-public-sync.sh"
  "${N03[@]}" "rm /tmp/nas02-public-sync.sh"

  if ! "${N03[@]}" "test -f $BASE/seeded"; then
    log "nas03: seeding $DS from RAID/Public by block cloning"
    seed_script | "${N03[@]}" "cat > /tmp/seed.py"
    sudo03 "bash -c 'python3 /tmp/seed.py /mnt/RAID/Public /mnt/$DS && date -Is > $BASE/seeded'"
    "${N03[@]}" "rm /tmp/seed.py; /sbin/zpool get -H bcloneused,bclonesaved RAID"
  fi

  local cron='{"user": "root", "command": "bash '"$BASE"'/nas02-public-sync.sh",
    "description": "nas03-mirror-public: nas02 Public -> '"$DS"'", "schedule": '"$SCHEDULE"', "stdout": false, "stderr": false}'
  local id; id=$("${N03[@]}" "midclt call cronjob.query" | python3 -c '
import json, sys
print(next((c["id"] for c in json.load(sys.stdin) if c["description"].startswith("nas03-mirror-public")), ""))')
  if [ -n "$id" ]; then "${N03[@]}" "midclt call cronjob.update $id $(printf %q "$cron")" >/dev/null; log "nas03: cron job $id updated"
  else "${N03[@]}" "midclt call cronjob.create $(printf %q "$cron")" >/dev/null; log "nas03: cron job created"; fi
  "${N03[@]}" "/sbin/zfs list -H -o name,used,refer RAID/Public $DS"
}

cron_id(){ "${N03[@]}" "midclt call cronjob.query" | python3 -c '
import json, sys
print(next(c["id"] for c in json.load(sys.stdin) if c["description"].startswith("nas03-mirror-public")))'; }

run() {
  "${N03[@]}" "midclt call cronjob.run $(cron_id)" >/dev/null
  log "sync started on nas03; ./nas03-mirror-public.sh status"
}

status() {
  "${N03[@]}" "pgrep -f '[n]as02-public-sync' >/dev/null && echo running || echo not-running
    tail -n 25 $BASE/nas02-public.log 2>/dev/null; echo \"last ok: \$(cat $BASE/last-ok 2>/dev/null || echo never)\"
    /sbin/zfs list -H -o name,used,refer $DS; /sbin/zfs list -H -t snapshot -o name,used -s creation $DS | tail -3"
}

verify() {
  : "${NAS03_SUDO_PW:?set NAS03_SUDO_PW (vault: projects/nas03)}"
  local out; out=$(sudo03 "rsync -aHAXn --numeric-ids --delete --itemize-changes \
    -e 'ssh -i $BASE/id_ed25519 -o BatchMode=yes -o UserKnownHostsFile=$BASE/known_hosts' \
    truenas_admin@$N02_TS:/ /mnt/$DS/" | grep -v '^\.d' || true)
  [ -z "$out" ] && log "nas03 mirror matches nas02's Public" || { head -20 <<<"$out"; die "$(wc -l <<<"$out") entries differ"; }
}

case ${1:-} in
  setup) setup ;;
  run) run ;;
  status) status ;;
  verify) verify ;;
  *) sed -n '2,17p' "$0"; exit 1 ;;
esac
