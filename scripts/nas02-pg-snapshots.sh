#!/usr/bin/env bash
# Hourly ZFS snapshots of nas02's shared Postgres data directory, kept 48h.
#
#   ./scripts/nas02-pg-snapshots.sh setup    # create or update the task, take one now
#   ./scripts/nas02-pg-snapshots.sh status   # the task and the newest snapshots
#
# PGDATA (the TrueNAS postgres app, host_path /mnt/SSD/postgres) is its own
# dataset, SSD/postgres, with pg_wal inside it. One snapshot of one dataset is
# atomic, so each is a crash-consistent copy that Postgres recovers from the way
# it recovers from a power cut. That covers the hours since the 03:30 dump, which
# the nightly pg_dump cannot. It is the same disk, though: the off-box copy is
# the dumps on nas03 (nas03-pgdumps.sh).
#
# Non-recursive, and named pg-hourly-* so TrueNAS's retention, which only
# deletes what matches the task's naming schema, can never touch democratic-csi's
# snapshots (those live under RAID/k8s and are named by the CSI snapshot id) or
# anything taken by hand.
#
# Restore: stop the postgres app, then
#   zfs rollback -r SSD/postgres@pg-hourly-<stamp>    (as root; -r drops newer ones)
# or, to keep the live data, clone it and point a second instance at the clone.
set -euo pipefail

DS=SSD/postgres
SCHEMA=pg-hourly-%Y-%m-%d_%H-%M
N02=(ssh -o BatchMode=yes -o ConnectTimeout=10 -i "$HOME/.ssh/nas" truenas_admin@"${N02_HOST:-nas02}")
mid(){ "${N02[@]}" "PATH=/usr/sbin:/sbin:\$PATH midclt call $*"; }

task_id(){ mid pool.snapshottask.query | python3 -c '
import json, sys
print(next((t["id"] for t in json.load(sys.stdin) if t["dataset"] == sys.argv[1] and t["naming_schema"] == sys.argv[2]), ""))' "$DS" "$SCHEMA"; }

setup() {
  local task id
  task=$(printf '{"dataset": "%s", "recursive": false, "exclude": [], "lifetime_value": 48, "lifetime_unit": "HOUR",
    "naming_schema": "%s", "allow_empty": true, "enabled": true,
    "schedule": {"minute": "0", "hour": "*", "dom": "*", "month": "*", "dow": "*", "begin": "00:00", "end": "23:59"}}' "$DS" "$SCHEMA")
  id=$(task_id)
  if [ -n "$id" ]; then mid pool.snapshottask.update "$id" "$(printf %q "$task")" >/dev/null; echo "task $id updated"
  else mid pool.snapshottask.create "$(printf %q "$task")" >/dev/null; id=$(task_id); echo "task $id created"; fi
  # Take the first one now instead of waiting for the top of the hour.
  mid pool.snapshottask.run "$id" >/dev/null
  sleep 5
  status
}

status() {
  mid pool.snapshottask.query | python3 -c '
import json, sys
for t in json.load(sys.stdin):
    if t["dataset"] == sys.argv[1]:
        print(t["id"], t["dataset"], t["naming_schema"], "keep", t["lifetime_value"], t["lifetime_unit"], "enabled" if t["enabled"] else "DISABLED", t["state"]["state"])' "$DS"
  "${N02[@]}" "/sbin/zfs list -H -t snapshot -o name,used,creation -s creation $DS | tail -3"
}

case ${1:-} in
  setup) setup ;;
  status) status ;;
  *) sed -n '2,5p' "$0"; exit 1 ;;
esac
