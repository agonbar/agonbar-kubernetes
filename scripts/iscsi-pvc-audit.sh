#!/usr/bin/env bash
# Audit the PVCs of a storage class against what the nightly job actually put on
# the backup share, so a volume is only retired once its data is provably
# somewhere else.
#
# Why this exists: "it is in the backup list" and "there is a restorable copy"
# are different claims, and this cluster has already been burned by the gap.
# The nightly run reports success per volume, but an rsync of an empty snapshot
# also succeeds. The only honest check is to go look at the destination.
#
# The share is read from a pod that NFS-mounts it as root, NOT over ssh. The
# first version of this script used ssh and was wrong in the worst possible
# direction: rsync -a preserves permissions, so a PGDATA arrives as drwx------
# owned by uid 70, the unprivileged ssh user gets EACCES, find counts zero
# files, and the script declares a perfectly good backup missing. It said
# dawarich and influxdb had no copy at all. They did.
#
# The freshness column is the newest mtime of any FILE in the copy, not when the
# copy ran: rsync -a preserves timestamps. For a volume nobody mounts any more
# that is the right number -- it says how old the data is, and it stops moving
# once the app stops.
#
#   ./iscsi-pvc-audit.sh                          # truenas-iscsi-ssd
#   ./iscsi-pvc-audit.sh --class truenas-iscsi-ssd-lan
#
# Verdicts:
#   MOUNTED    a running pod has it; nothing to decide here
#   SAFE       no pod mounts it and the backup share holds files for it
#   NO-BACKUP  no pod mounts it and the copy is missing or empty -- look before
#              you delete, the volume may hold the only copy of something
set -euo pipefail

CONTEXT="${CONTEXT:-lamg}"
CLASS="truenas-iscsi-ssd"
NFS_SERVER="${NFS_SERVER:-192.168.0.29}"
NFS_PATH="${NFS_PATH:-/mnt/RAID/docker/backups-iscsi}"
POD="iscsi-audit-$$"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --class) CLASS="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

k() { kubectl --context "$CONTEXT" "$@"; }
cleanup() { k -n backup delete pod "$POD" --ignore-not-found --now >/dev/null 2>&1 || true; }
trap cleanup EXIT

cat <<EOF | k apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: $POD
  namespace: backup
spec:
  restartPolicy: Never
  containers:
    - name: audit
      image: alpine:latest
      command:
        - sh
        - -c
        - |
          # busybox find has no -printf and no -newermt, and it fails quietly:
          # the timestamp field comes back empty, every line loses a column, and
          # the parser reads the whole share as missing. GNU find first.
          apk add --no-cache findutils >/dev/null 2>&1 || exit 1
          cd /backup || exit 1
          for d in */*/; do
            # -print -quit stops at the first file instead of walking the tree.
            # Counting every file and du -sb'ing the whole share is what made the
            # first version unusable: lamg/plex-config alone is 165k files over
            # NFS and the pod never finished. The question here is only whether a
            # restorable copy exists, and one file answers it.
            f=\$(/usr/bin/find "\$d" -type f -print -quit 2>/dev/null)
            [ -n "\$f" ] && n=1 || n=0
            # Freshness capped at depth 3 for the same reason. Some trees keep
            # every file deeper than that -- plex-config starts at Library/
            # Application Support/Plex Media Server/... -- so fall back to the
            # mtime of the copy's own directory, which rsync moves whenever it
            # changes anything. That makes this column a floor, not an exact
            # date, and a floor is all the retire decision needs.
            t=\$(/usr/bin/find "\$d" -maxdepth 3 -type f -printf '%T@\n' 2>/dev/null | sort -n | tail -1)
            t=\${t%%.*}
            [ -n "\$t" ] || t=\$(/usr/bin/find "\$d" -maxdepth 0 -printf '%T@' 2>/dev/null | cut -d. -f1)
            echo "\${d%/} \$n \${t:-0}"
          done
      volumeMounts:
        - { name: backup, mountPath: /backup, readOnly: true }
  volumes:
    - name: backup
      nfs:
        server: $NFS_SERVER
        path: $NFS_PATH
  # 192.168.0.29 is the LAN leg of nas02 and only the home pool routes to it.
  nodeSelector:
    svccontroller.k3s.cattle.io/lbpool: lamg
EOF

for _ in $(seq 1 60); do
  phase=$(k -n backup get pod "$POD" -o jsonpath='{.status.phase}' 2>/dev/null || true)
  [[ "$phase" == "Succeeded" || "$phase" == "Failed" ]] && break
  sleep 5
done
[[ "${phase:-}" == "Succeeded" ]] || { echo "the audit pod ended in ${phase:-unknown}" >&2; k -n backup logs "$POD" >&2 || true; exit 1; }
SHARE=$(k -n backup logs "$POD")

k get pvc -A -o json | CLASS="$CLASS" SHARE="$SHARE" CONTEXT="$CONTEXT" python3 -c '
import json, os, subprocess, sys, datetime

share = {}
for line in os.environ["SHARE"].splitlines():
    p = line.split()
    if len(p) == 3:
        share[p[0]] = (int(p[1]), int(p[2] or 0))

pods = json.loads(subprocess.run(
    ["kubectl", "--context", os.environ["CONTEXT"], "get", "pods", "-A", "-o", "json"],
    capture_output=True, text=True).stdout)["items"]
mounted = set()
for po in pods:
    if po["status"].get("phase") in ("Succeeded", "Failed"):
        continue
    for v in po["spec"].get("volumes", []):
        c = v.get("persistentVolumeClaim")
        if c:
            mounted.add((po["metadata"]["namespace"], c["claimName"]))

rows, safe, nobackup = [], [], []
for p in json.load(sys.stdin)["items"]:
    if p["spec"].get("storageClassName") != os.environ["CLASS"]:
        continue
    ns, name = p["metadata"]["namespace"], p["metadata"]["name"]
    files, ts = share.get("%s/%s" % (ns, name), (0, 0))
    when = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "-"
    if (ns, name) in mounted:
        verdict = "MOUNTED"
    elif files > 0:
        verdict = "SAFE"; safe.append("%s/%s" % (ns, name))
    else:
        verdict = "NO-BACKUP"; nobackup.append("%s/%s" % (ns, name))
    rows.append(("%s/%s" % (ns, name), p["spec"]["resources"]["requests"]["storage"],
                 verdict, "yes" if files else "no", when))

for r in sorted(rows, key=lambda r: (r[2], r[0])):
    print("%-38s %-6s %-10s  copy=%-4s newest=%s" % r)
print()
nmounted = sum(1 for r in rows if r[2] == "MOUNTED")
print("%d PVCs on %s: %d mounted, %d safe to retire, %d without a backup"
      % (len(rows), os.environ["CLASS"], nmounted, len(safe), len(nobackup)))
if nobackup:
    print("no backup: " + " ".join(nobackup))
'
