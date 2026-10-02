#!/usr/bin/env bash
# nas03 pulls a read-only mirror of nas02's datasets every night, over the tailnet,
# into RAID/mirror/nas02-<name>, with one ZFS snapshot per run (last 30 kept).
#
#   NAS02_SUDO=... NAS03_SUDO=... ./nas03-mirror.sh setup   # keys, datasets, script, cron
#   NAS02_SUDO=... NAS03_SUDO=... ./nas03-mirror.sh seed    # block-clone nas03's RAID/Public into the new datasets
#   NAS03_SUDO=... ./nas03-mirror.sh run                    # start a sync now (transient unit on nas03)
#   NAS03_SUDO=... ./nas03-mirror.sh status
#
# Sudo passwords live in the vault (projects/nas03, projects/plex-nas02), never here.
#
# Each mirror has its own key on nas03 (/mnt/RAID/mirror/id_<name>), authorized on
# nas02 with a forced `rrsync -ro <dir>`: no shell, no writes, one directory.
# Public reads as truenas_admin. adrian and nas00-rescate read as root, because
# they hold 770 files owned by root/lamg/adrian that any other user would skip.
#
# `seed` exists because nas03's RAID/Public (the nas00 seed of 2026-09) already has
# most of these files under the same or another path. It lists nas02 through the
# mirror key, matches by path or basename+size, and clones with copy_file_range
# (ZFS block cloning: no space, no network). rsync then fixes whatever differs.
set -euo pipefail

NAS02_IP=100.72.0.41
D=/mnt/RAID/mirror
# name  nas02-user  nas02-dir  nas03-dataset
MIRRORS=(
  "public truenas_admin /mnt/RAID/Public RAID/mirror/nas02-Public"
  "adrian root /mnt/RAID/adrian RAID/mirror/nas02-adrian"
  "nas00-rescate root /mnt/RAID/nas00-rescate RAID/mirror/nas02-nas00-rescate"
)
N02=(ssh -o BatchMode=yes -i "$HOME/.ssh/nas" truenas_admin@"$NAS02_IP")
N03=(ssh -o BatchMode=yes nas03)

log(){ echo "[$(date +%H:%M:%S)] $*"; }
# sudo -S eats the first stdin line, so file contents go up in a separate ssh call.
s02(){ printf '%s\n' "${NAS02_SUDO:?}" | "${N02[@]}" "sudo -S -p '' sh -c '$1'"; }
s03(){ printf '%s\n' "${NAS03_SUDO:?}" | "${N03[@]}" "sudo -S -p '' sh -c '$1'"; }
put03(){ "${N03[@]}" "cat > $2" < "$1"; }

sync_script() {
  local entries=""
  for m in "${MIRRORS[@]}"; do
    read -r name user _ ds <<<"$m"
    entries+="  \"$name $user $ds\""$'\n'
  done
  cat <<EOF
#!/bin/bash
# Installed by agonbar-kubernetes scripts/nas03-mirror.sh. Edit there, not here.
set -uo pipefail
D=$D
exec 9>\$D/.lock; flock -n 9 || { echo "already running"; exit 0; }
exec > \$D/nas02-sync.log 2>&1
MIRRORS=(
$entries)
fail=0
for m in "\${MIRRORS[@]}"; do
  read -r name user ds <<<"\$m"
  echo "== \$(date -Is) \$name start"
  rsync -aHAX --numeric-ids --delete --partial-dir=.rsync-partial --info=stats2 \\
    -e "ssh -i \$D/id_\$name -o BatchMode=yes -o UserKnownHostsFile=\$D/known_hosts -o ServerAliveInterval=30" \\
    "\$user@$NAS02_IP:/" "/mnt/\$ds/"
  rc=\$?
  echo "== \$(date -Is) \$name rsync exit \$rc"
  # 24 = files vanished on nas02 mid-run, normal on a live share.
  if [ \$rc -ne 0 ] && [ \$rc -ne 24 ]; then fail=1; continue; fi
  /sbin/zfs snapshot "\$ds@mirror-\$(date +%Y%m%d-%H%M)"
  /sbin/zfs list -H -t snapshot -o name -s creation "\$ds" | grep '@mirror-' | head -n -30 | xargs -r -n1 /sbin/zfs destroy
  date -Is > "\$D/last-ok-\$name"
done
exit \$fail
EOF
}

setup() {
  # Public's key predates this script as id_ed25519.
  s03 "cd $D && if [ -f id_ed25519 ]; then mv id_ed25519 id_public && mv id_ed25519.pub id_public.pub; fi"
  for m in "${MIRRORS[@]}"; do
    read -r name user dir ds <<<"$m"
    s03 "[ -f $D/id_$name ] || ssh-keygen -q -t ed25519 -N \"\" -C nas03-mirror-$name -f $D/id_$name"
    local pub; pub=$(s03 "cat $D/id_$name.pub")
    log "nas02: authorize $name for $user, rrsync -ro $dir"
    "${N02[@]}" "cat > /tmp/nas03-authorize.py" <<'EOF'
import json, subprocess, sys
user, d, pub = sys.argv[1:]
mid = lambda *a: subprocess.run(["midclt", "call", *a], check=True, capture_output=True, text=True).stdout
u = json.loads(mid("user.query", json.dumps([["username", "=", user]])))[0]
blob = pub.split()[1]
keys = [k for k in (u["sshpubkey"] or "").splitlines() if k.strip() and blob not in k]
keys.append(f'command="/usr/bin/rrsync -ro {d}",restrict {pub}')
mid("user.update", str(u["id"]), json.dumps({"sshpubkey": "\n".join(keys)}))
EOF
    s02 "python3 /tmp/nas03-authorize.py $user $dir \"$pub\""
    if ! s03 "zfs list -H $ds >/dev/null 2>&1"; then
      log "nas03: create $ds"
      s03 "midclt call pool.dataset.create '\''{\"name\": \"$ds\"}'\'' >/dev/null"
    fi
  done
  sync_script > /tmp/nas02-sync.sh
  put03 /tmp/nas02-sync.sh /tmp/nas02-sync.sh
  s03 "install -m 700 /tmp/nas02-sync.sh $D/nas02-sync.sh"
  local id; id=$(s03 "midclt call cronjob.query" | python3 -c 'import json,sys; print(next((j["id"] for j in json.load(sys.stdin) if "/mnt/RAID/mirror/" in j["command"]), ""))')
  local job='{"command": "bash '$D'/nas02-sync.sh", "user": "root", "schedule": {"minute": "0", "hour": "2", "dom": "*", "month": "*", "dow": "*"}, "enabled": true, "description": "nas02 -> nas03 mirror"}'
  if [ -n "$id" ]; then s03 "midclt call cronjob.update $id '\''$job'\'' >/dev/null"
  else s03 "midclt call cronjob.create '\''$job'\'' >/dev/null"; fi
  s03 "rm -f $D/nas02-public-sync.sh"
  log "nas03: cron at 02:00 -> $D/nas02-sync.sh"
}

seed() {
  cat > /tmp/nas03-seed-mirror.py <<'EOF'
import collections, os, re, shutil, subprocess, sys
name, user, ds, ip = sys.argv[1:]
D, SRC, DST = "/mnt/RAID/mirror", "/mnt/RAID/Public", "/mnt/" + ds
out = subprocess.run(["rsync", "-r", "-8", "--list-only", "-e",
                      f"ssh -i {D}/id_{name} -o BatchMode=yes -o UserKnownHostsFile={D}/known_hosts",
                      f"{user}@{ip}:/"], check=True, capture_output=True).stdout
line = re.compile(rb"^-\S{9}\s+([\d,.]+) \S+ \S+ (.*)$")
remote = {}
for l in out.split(b"\n"):
    m = line.match(l)
    if m:
        remote[os.fsdecode(m.group(2))] = int(m.group(1).replace(b",", b"").replace(b".", b""))
local, by_name = {}, collections.defaultdict(list)
for root, _, files in os.walk(SRC):
    for f in files:
        p = os.path.join(root, f)
        s = os.lstat(p).st_size
        rel = os.path.relpath(p, SRC)
        local[rel] = s
        by_name[(f, s)].append(rel)
done = skipped = nbytes = 0
for rel, s in remote.items():
    dst = os.path.join(DST, rel)
    if os.path.lexists(dst):
        skipped += 1
        continue
    src = rel if local.get(rel) == s else (by_name.get((os.path.basename(rel), s)) or [None])[0]
    if src is None or s == 0:
        continue
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(os.path.join(SRC, src), "rb") as fi, open(dst, "wb") as fo:
        left = s
        while left:
            n = os.copy_file_range(fi.fileno(), fo.fileno(), left)
            if n == 0:
                break
            left -= n
    shutil.copystat(os.path.join(SRC, src), dst)
    done += 1
    nbytes += s
total = sum(remote.values())
print(f"{name}: nas02 {len(remote)} files {total / 2**40:.2f}T; cloned {done} files {nbytes / 2**40:.2f}T; "
      f"already there {skipped}; left for rsync {(total - nbytes) / 2**40:.2f}T")
EOF
  put03 /tmp/nas03-seed-mirror.py /tmp/nas03-seed-mirror.py
  for m in "${MIRRORS[@]}"; do
    read -r name user _ ds <<<"$m"
    [ "$name" = public ] && continue   # seeded on 2026-10-02, before this script
    log "seed $name"
    s03 "python3 /tmp/nas03-seed-mirror.py $name $user $ds $NAS02_IP"
  done
  s03 "zpool get -H -o value bcloneused,bclonesaved RAID" | paste -sd' ' | sed 's/^/bcloneused bclonesaved: /'
}

run() {
  s03 "systemd-run --unit nas02-mirror-manual --collect bash $D/nas02-sync.sh"
  log "started; follow with: $0 status"
}

status() {
  s03 "systemctl is-active nas02-mirror-manual 2>/dev/null; tail -n 15 $D/nas02-sync.log 2>/dev/null; grep . $D/last-ok-* 2>/dev/null; zfs list -o name,used,refer -r RAID/mirror"
}

"${1:?usage: $0 setup|seed|run|status}"
