#!/usr/bin/env bash
# nas03 pulls the nightly Postgres dumps of nas02's shared server off nas02, so
# losing nas02 does not take the database and every copy of it at once. The
# dumps are written by piracy-postgres-backup at 03:30 onto the PVC
# piracy-pg-backups, which democratic-csi puts on nas02's own RAID pool.
#
#   ./scripts/nas03-pgdumps.sh setup    # dataset, key, nas02 grant, Kuma URL, cron
#   ./scripts/nas03-pgdumps.sh run      # pull now, what the 04:30 cron does
#   ./scripts/nas03-pgdumps.sh status   # last log, newest set, space used
#
# Same scheme as nas03-mirror.sh: nas03 pulls over the tailnet with its own key,
# which nas02 only lets run `rrsync -ro <the PVC directory>`. The difference is
# retention. This is not a mirror: there is no --delete, so a file nas02 drops
# after its 14 days stays on nas03, which prunes by dump time after KEEP_DAYS.
# A wiped or encrypted share on nas02 cannot reach back and empty nas03.
#
# Everything runs as truenas_admin on both sides, so setup needs no sudo. Rerun
# setup if the PVC is ever recreated: the source path is read from the cluster.
#
# Each pull pings the Kuma monitor "backup: piracy pg dumps on nas03"
# (scripts/uptime-backup-monitors.sh, run that first): up when tonight's set
# arrived, down when the pull failed or the newest set is stale.
set -euo pipefail

CTX=lamg
NAS02_IP=100.72.0.41    # nas02 on the tailnet, as seen from nas03
DS=RAID/mirror/nas02-pgdumps
D=/mnt/$DS
KEEP_DAYS=30
MONITOR="backup: piracy pg dumps on nas03"
SCHEDULE='{"minute": "30", "hour": "4", "dom": "*", "month": "*", "dow": "*"}'
# nas02 by name: the tailnet resolves it, 192.168.0.29 only answers on the home LAN.
N02=(ssh -o BatchMode=yes -o ConnectTimeout=10 -i "$HOME/.ssh/nas" truenas_admin@"${N02_HOST:-nas02}")
N03=(ssh -o BatchMode=yes -o ConnectTimeout=10 "${N03_HOST:-nas03}")

log(){ echo "[$(date +%H:%M:%S)] $*"; }
die(){ echo "FATAL: $*" >&2; exit 1; }

pull_script() {
  cat <<EOF
#!/bin/bash
# Installed by agonbar-kubernetes scripts/nas03-pgdumps.sh. Edit there, not here.
set -uo pipefail
D=$D
exec 9>\$D/.lock; flock -n 9 || { echo "already running"; exit 0; }
exec > \$D/pull.log 2>&1
echo "== \$(date -Is) start"
kuma() { curl -fsS -m 20 -o /dev/null "\$(cat \$D/.kuma-url)?status=\$1&msg=\$2" && echo "kuma \$1: ok" || echo "kuma ping failed"; }

mkdir -p \$D/dumps
rsync -rt --chmod=D700,F600 --include='*.dump' --include='globals-*.sql' --exclude='*' --info=stats1 \\
  -e "ssh -i \$D/.ssh/id_pgdumps -o BatchMode=yes -o UserKnownHostsFile=/mnt/RAID/mirror/known_hosts -o ServerAliveInterval=30" \\
  truenas_admin@$NAS02_IP:/ \$D/dumps/
rc=\$?
echo "== \$(date -Is) rsync exit \$rc"
[ \$rc -eq 0 ] || { kuma down "rsync+exit+\$rc"; exit \$rc; }

# rsync -t kept the dump time, so this ages files from when they were dumped.
find \$D/dumps -type f -mtime +$KEEP_DAYS -print -delete

# The newest set has to be tonight's and no smaller than the one before it. One
# timestamp names a whole set: globals-<ts>.sql plus <db>-<ts>.dump per database.
sets=(\$(ls \$D/dumps/globals-*.sql 2>/dev/null | sed 's/.*globals-//; s/\.sql\$//' | sort -r))
[ \${#sets[@]} -gt 0 ] || { kuma down "no+dumps"; exit 1; }
n=\$(ls \$D/dumps/*-\${sets[0]}.dump 2>/dev/null | wc -l)
prev=\$( [ \${#sets[@]} -gt 1 ] && ls \$D/dumps/*-\${sets[1]}.dump | wc -l || echo 0)
age=\$(( (\$(date +%s) - \$(stat -c %Y \$D/dumps/globals-\${sets[0]}.sql)) / 3600 ))
echo "newest set \${sets[0]}: \$n dumps (previous \$prev), \${age}h old, \$(du -sh \$D/dumps | cut -f1) kept"
if [ \$age -ge 20 ]; then kuma down "newest+set+\${age}h+old"; exit 1; fi
if [ \$n -eq 0 ] || [ \$n -lt \$prev ]; then kuma down "set+\${sets[0]}+has+\$n+dumps,+previous+\$prev"; exit 1; fi
kuma up "\$n+dumps+\${sets[0]}"
date -Is > \$D/last-ok
EOF
}

setup() {
  local pv src
  pv=$(kubectl --context "$CTX" -n piracy get pvc piracy-pg-backups -o jsonpath='{.spec.volumeName}')
  src=$(kubectl --context "$CTX" get pv "$pv" -o jsonpath='{.spec.csi.volumeAttributes.share}')
  [ -n "$src" ] || die "could not resolve the share behind piracy-pg-backups"
  log "source: nas02:$src"

  if "${N03[@]}" "midclt call pool.dataset.query '[[\"id\",\"=\",\"$DS\"]]'" | grep -q '"id"'; then
    log "nas03: $DS already exists"
  else
    "${N03[@]}" "midclt call pool.dataset.create '{\"name\": \"$DS\"}'" >/dev/null
    "${N03[@]}" "midclt call -j filesystem.setperm '{\"path\": \"$D\", \"uid\": 950, \"gid\": 950, \"mode\": \"700\"}'" >/dev/null
    log "nas03: created $DS, owned by truenas_admin, mode 700"
  fi

  "${N03[@]}" "mkdir -p -m 700 $D/.ssh $D/dumps && { [ -f $D/.ssh/id_pgdumps ] || ssh-keygen -q -t ed25519 -N '' -C nas03-pgdumps -f $D/.ssh/id_pgdumps; }"
  local pub; pub=$("${N03[@]}" "cat $D/.ssh/id_pgdumps.pub")

  "${N02[@]}" "cat > /tmp/nas03-pgdumps-authorize.py" <<'EOF'
import json, subprocess, sys
d, pub = sys.argv[1:]
mid = lambda *a: subprocess.run(["midclt", "call", *a], check=True, capture_output=True, text=True).stdout
u = json.loads(mid("user.query", json.dumps([["username", "=", "truenas_admin"]])))[0]
blob = pub.split()[1]
keys = [k for k in (u["sshpubkey"] or "").splitlines() if k.strip() and blob not in k]
keys.append(f'command="/usr/bin/rrsync -ro {d}",restrict {pub}')
mid("user.update", str(u["id"]), json.dumps({"sshpubkey": "\n".join(keys)}))
print(f"nas02: truenas_admin has {len(keys)} keys, nas03-pgdumps restricted to rrsync -ro {d}")
EOF
  "${N02[@]}" "export PATH=/usr/sbin:/sbin:\$PATH; python3 /tmp/nas03-pgdumps-authorize.py $src $(printf %q "$pub"); rm /tmp/nas03-pgdumps-authorize.py"

  # The push URL goes over stdin straight into a 600 file, never onto a command line.
  local token domain
  token=$(printf "select push_token from monitor where name='%s';\n" "$MONITOR" |
    kubectl --context "$CTX" -n agonbar exec -i deploy/uptime -- sqlite3 -readonly /app/data/kuma.db)
  [ -n "$token" ] || die "no Kuma monitor '$MONITOR'; run scripts/uptime-backup-monitors.sh first"
  domain=$(kubectl --context "$CTX" -n argocd get secret argocd-cmp-values -o jsonpath='{.data.BASE_DOMAIN}' | base64 -d)
  printf 'https://uptime.%s/api/push/%s\n' "$domain" "$token" |
    "${N03[@]}" "umask 077; cat > $D/.kuma-url"
  log "nas03: Kuma push URL stored in $D/.kuma-url"

  pull_script | "${N03[@]}" "cat > $D/pull.sh && chmod 700 $D/pull.sh"

  local job id
  job='{"command": "bash '"$D"'/pull.sh", "user": "truenas_admin", "schedule": '"$SCHEDULE"',
    "enabled": true, "stdout": false, "stderr": false, "description": "nas03-pgdumps: nas02 Postgres dumps -> '"$DS"'"}'
  id=$("${N03[@]}" "midclt call cronjob.query" | python3 -c '
import json, sys
print(next((j["id"] for j in json.load(sys.stdin) if j["description"].startswith("nas03-pgdumps")), ""))')
  if [ -n "$id" ]; then "${N03[@]}" "midclt call cronjob.update $id $(printf %q "$job")" >/dev/null; log "nas03: cron job $id updated"
  else "${N03[@]}" "midclt call cronjob.create $(printf %q "$job")" >/dev/null; log "nas03: cron job created, 04:30"; fi
}

run() {
  "${N03[@]}" "bash $D/pull.sh; tail -n 12 $D/pull.log"
}

status() {
  "${N03[@]}" "tail -n 12 $D/pull.log 2>/dev/null; echo \"last ok: \$(cat $D/last-ok 2>/dev/null || echo never)\"
    ls $D/dumps | sed 's/.*-\([0-9]\{8\}\)-[0-9]\{6\}\..*/\1/' | sort | uniq -c | tail -3
    /sbin/zfs list -H -o name,used,avail $DS"
}

case ${1:-} in
  setup) setup ;;
  run) run ;;
  status) status ;;
  *) sed -n '2,8p' "$0"; exit 1 ;;
esac
