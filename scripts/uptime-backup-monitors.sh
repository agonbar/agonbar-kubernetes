#!/usr/bin/env bash
# Uptime Kuma push monitors for the nightly backups, so a backup that fails, or
# never runs at all, reaches the phone through Kuma's default notification
# (Telegram) instead of being spotted days later.
#
#   ./scripts/uptime-backup-monitors.sh          # create missing monitors, seal their tokens
#   ./scripts/uptime-backup-monitors.sh status   # last heartbeat of each
#
# Each job pings its monitor with status=up when it finishes clean and status=down
# when it fails. A monitor with no ping for HOURS goes down by itself, which is
# what catches the run that never started (the August 2026 nas00 pin, a suspended
# CronJob, a pod that cannot schedule).
#
# There are no Kuma API credentials anywhere in the vault, so this writes the
# rows straight into kuma.db and restarts the container in place (kill 1 under
# dumb-init) so Kuma loads them. kill 1 rather than a rollout: the Deployment
# surges, and two Kumas on one SQLite file is how you get a corrupt database.
# A new monitor gets a seeded UP heartbeat, because a push monitor with no
# history goes down (and pages) on its very first check.
#
# Tokens are secrets (anyone holding one can keep a dead backup green), so they
# go to the cluster as a SealedSecret `uptime-push`, key `token`, appended to
# <dir>/sealedsecrets.yaml. The nas03 monitor has no k8s side: nas03-pgdumps.sh
# reads its token from kuma.db when it installs the pull.
set -euo pipefail
cd "$(dirname "$0")/.."

CTX=lamg
DB=/app/data/kuma.db
KUMA=(kubectl --context "$CTX" -n agonbar exec -i deploy/uptime --)
# name | hours without a ping before it goes down | namespace | repo dir of its sealedsecrets.yaml
MONITORS=(
  "backup: piracy-postgres-backup|26|piracy|deployments/piracy"
  "backup: immich-postgres-backup|26|immich|deployments/immich"
  "backup: dawarich-postgres-backup|26|dawarich|deployments/dawarich"
  # pvc-backup runs 1-3h depending on what changed, so a little more slack.
  "backup: pvc-backup|28|backup|system/snapshots"
  "backup: piracy pg dumps on nas03|26|-|-"
  # Not a backup: the daily check of the litellm fork against HA core
  # (deployments/lamg/ha-litellm-drift.yaml), same push mechanics.
  "ha: litellm fork vs core|26|lamg|deployments/lamg"
)

log(){ echo "[$(date +%H:%M:%S)] $*"; }
sql(){ "${KUMA[@]}" sqlite3 "$DB"; }
q(){ printf '%s\n' "$1" | "${KUMA[@]}" sqlite3 -readonly "$DB"; }
esc(){ printf '%s' "${1//\'/\'\'}"; }

# Replace the SealedSecret named $2 in file $1 with the document on stdin, or append it.
put_sealed() {
  local doc; doc=$(cat)
  [ -n "$doc" ] || { echo "FATAL: kubeseal produced nothing for $1"; exit 1; }
  # The script comes from the heredoc, so the document goes as an argument, not stdin.
  python3 - "$1" "$2" "$doc" <<'EOF'
import os, re, sys
path, name, new = sys.argv[1], sys.argv[2], sys.argv[3]
new = re.sub(r"\A---\s*\n", "", new.strip()) + "\n"
text = open(path).read() if os.path.exists(path) else ""
# Split on the separators but keep them, so every other document stays byte for byte.
chunks = re.split(r"(?m)(^---[ \t]*\n)", text)
hit = [i for i, c in enumerate(chunks) if re.search(r"(?m)^kind: SealedSecret$", c)
       and re.search(r"(?m)^  name: %s$" % re.escape(name), c)]
if hit:
    chunks[hit[0]] = new
    text = "".join(chunks)
else:
    text = (text.rstrip("\n") + "\n" if text.strip() else "") + "---\n" + new
open(path, "w").write(text)
EOF
}

apply() {
  created=0
  for m in "${MONITORS[@]}"; do
    IFS='|' read -r name hours _ _ <<<"$m"
    if [ -n "$(q "select id from monitor where name='$(esc "$name")'")" ]; then
      log "kuma: '$name' exists"
      continue
    fi
    [ "$created" = 1 ] || { q ".backup /app/data/kuma.db.bak-$(date +%Y%m%d%H%M%S)"; log "kuma: db backed up"; }
    token=$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32 || true)
    secs=$((hours * 3600))
    sql <<EOF
BEGIN;
INSERT INTO monitor (name, type, active, user_id, interval, retry_interval, maxretries, push_token, description)
  VALUES ('$(esc "$name")', 'push', 1, (SELECT min(id) FROM user), $secs, $secs, 0, '$token',
          'Down after ${hours}h without a ping. Managed by agonbar-kubernetes scripts/uptime-backup-monitors.sh');
INSERT INTO monitor_notification (monitor_id, notification_id)
  SELECT last_insert_rowid(), id FROM notification WHERE is_default = 1 AND active = 1;
INSERT INTO heartbeat (monitor_id, status, msg, time, important, duration)
  SELECT id, 1, 'created, waiting for the first ping', strftime('%Y-%m-%d %H:%M:%f', 'now'), 1, 0
  FROM monitor WHERE push_token = '$token';
COMMIT;
EOF
    log "kuma: created '$name' (${hours}h)"
    created=1
  done
  if [ "$created" = 1 ]; then
    log "kuma: restarting the container in place to load the new monitors"
    "${KUMA[@]}" sh -c 'kill 1' || true
    sleep 10
    up='require("http").get("http://127.0.0.1:3001/", () => process.exit(0)).on("error", () => process.exit(1))'
    for _ in $(seq 1 40); do "${KUMA[@]}" node -e "$up" 2>/dev/null && break; sleep 3; done
    "${KUMA[@]}" node -e "$up" || { echo "FATAL: Kuma did not come back"; exit 1; }
    log "kuma: back up"
  fi

  for m in "${MONITORS[@]}"; do
    IFS='|' read -r name _ ns dir <<<"$m"
    [ "$ns" = - ] && continue
    token=$(q "select push_token from monitor where name='$(esc "$name")'")
    live=$(kubectl --context "$CTX" -n "$ns" get secret uptime-push -o jsonpath='{.data.token}' 2>/dev/null | base64 -d || true)
    if [ "$live" = "$token" ] && grep -q '^  name: uptime-push$' "$dir/sealedsecrets.yaml" 2>/dev/null; then
      log "$ns: uptime-push already holds the token"
      continue
    fi
    kubectl create secret generic uptime-push -n "$ns" --from-literal=token="$token" --dry-run=client -o yaml |
      kubeseal --context "$CTX" --scope cluster-wide -o yaml | put_sealed "$dir/sealedsecrets.yaml" uptime-push
    log "$ns: sealed uptime-push into $dir/sealedsecrets.yaml (commit it)"
  done
}

status() {
  q "select m.name, (m.interval/3600) || 'h', case h.status when 1 then 'UP' when 0 then 'DOWN' else h.status end, h.time || 'Z', h.msg
     from monitor m left join heartbeat h on h.id = (select max(id) from heartbeat where monitor_id = m.id)
     where m.type = 'push' order by m.name"
}

case ${1:-apply} in
  apply) apply ;;
  status) status ;;
  *) sed -n '2,8p' "$0"; exit 1 ;;
esac
