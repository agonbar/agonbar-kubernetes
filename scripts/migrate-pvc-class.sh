#!/usr/bin/env bash
# Move a workload's PVC to a different StorageClass, data intact.
#
#   ./migrate-pvc-class.sh <ns> <deploy/name> <old-pvc> <new-pvc> <new-class> [container-path-hint]
#
# Quiesces the workload (so the copy is consistent without a dump), provisions the
# new claim, copies with rsync from a pod that mounts both, and refuses to finish
# unless the two trees hash identically. Leaves the old claim untouched as the
# rollback: it does NOT repoint the Deployment. Do that in git, which is where
# ArgoCD reads from, then scale back up.
#
# Suspends the namespace's ArgoCD app while it runs, because selfHeal will happily
# scale the workload back up mid-copy. Restores it on the way out, always.
set -euo pipefail
CTX=${CTX:-lamg}
NS=$1; WL=$2; OLD=$3; NEW=$4; CLASS=$5
K="kubectl --context $CTX"
POD="pvcmig-${OLD}"
log(){ echo "[$(date +%H:%M:%S)] $*"; }
die(){ echo "FATAL: $*" >&2; exit 1; }

# Two runs in the same namespace fight over one ArgoCD app: the first to finish
# re-enables autosync and selfHeal scales the other workload back up mid-copy,
# re-attaching the volume being read. The md5 gate catches the bad copy, but the
# run is wasted. Refuse to start instead.
for other in /tmp/.pvcmig-$NS.lock; do
  if [ -e "$other" ] && kill -0 "$(cat "$other" 2>/dev/null)" 2>/dev/null; then
    die "another migration is already running in ns/$NS (pid $(cat "$other")); they would fight over the ArgoCD app"
  fi
done
echo $$ > "/tmp/.pvcmig-$NS.lock"

APP=$($K -n argocd get app "$NS" -o name >/dev/null 2>&1 && echo "$NS" || echo "")
REPLICAS=$($K -n "$NS" get "$WL" -o jsonpath='{.spec.replicas}')
SIZE=$($K -n "$NS" get pvc "$OLD" -o jsonpath='{.spec.resources.requests.storage}')
log "ns=$NS wl=$WL $OLD -> $NEW ($CLASS, $SIZE) replicas=$REPLICAS app=${APP:-<none>}"

restore(){
  rm -f "/tmp/.pvcmig-$NS.lock"
  log "restoring: temp pod gone, replicas=$REPLICAS, autosync"
  $K -n "$NS" delete pod "$POD" --ignore-not-found --wait=true >/dev/null 2>&1 || true
  $K -n "$NS" scale "$WL" --replicas="$REPLICAS" >/dev/null 2>&1 || true
  [ -n "$APP" ] && $K -n argocd patch app "$APP" --type=merge \
    -p '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}' >/dev/null 2>&1 || true
}
trap restore EXIT

[ -n "$APP" ] && $K -n argocd patch app "$APP" --type=merge \
  -p '{"spec":{"syncPolicy":{"automated":null}}}' >/dev/null
log "quiescing $WL"
$K -n "$NS" scale "$WL" --replicas=0 >/dev/null
SEL=$($K -n "$NS" get "$WL" -o jsonpath='{.spec.selector.matchLabels}' | tr -d '{}"' | tr ',' '\n' | paste -sd, -)
until [ -z "$($K -n "$NS" get pods -l "$SEL" --no-headers 2>/dev/null)" ]; do sleep 3; done
VOL=$($K -n "$NS" get pvc "$OLD" -o jsonpath='{.spec.volumeName}')
until ! $K get volumeattachment -o jsonpath='{.items[*].spec.source.persistentVolumeName}' 2>/dev/null \
      | tr ' ' '\n' | grep -qx "$VOL"; do sleep 5; done
log "old volume detached"

cat <<EOF | $K apply -f - >/dev/null
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: $NEW, namespace: $NS}
spec:
  accessModes: [$( [ "${CLASS#*nfs}" != "$CLASS" ] && echo ReadWriteMany || echo ReadWriteOnce )]
  storageClassName: $CLASS
  resources: {requests: {storage: $SIZE}}
EOF
log "claim $NEW created"

$K -n "$NS" delete pod "$POD" --ignore-not-found --wait=true >/dev/null 2>&1 || true
cat <<EOF | $K apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata: {name: $POD, namespace: $NS}
spec:
  restartPolicy: Never
  nodeSelector: {svccontroller.k3s.cattle.io/lbpool: lamg}
  containers:
    - name: c
      image: alpine:latest
      command: ["sleep", "7200"]
      volumeMounts:
        - {name: old, mountPath: /old}
        - {name: new, mountPath: /new}
  volumes:
    - {name: old, persistentVolumeClaim: {claimName: $OLD}}
    - {name: new, persistentVolumeClaim: {claimName: $NEW}}
EOF
until $K -n "$NS" get pod "$POD" --no-headers 2>/dev/null | grep -q Running; do sleep 4; done
log "copying ($($K -n "$NS" exec "$POD" -- du -sh /old | cut -f1), $($K -n "$NS" exec "$POD" -- sh -c 'find /old -type f | wc -l') files)"

$K -n "$NS" exec "$POD" -- sh -c 'apk add --no-cache rsync >/dev/null 2>&1
  rsync -aHAX --numeric-ids --delete --exclude "lost+found" /old/ /new/' \
  || die "rsync failed"

# A file count is not proof. Hash both trees and refuse to pass unless they match.
log "verifying"
$K -n "$NS" exec "$POD" -- sh -c '
  SRC=$(cd /old && find . -type f -not -path "./lost+found/*" | sort | xargs md5sum 2>/dev/null | md5sum | cut -d" " -f1)
  DST=$(cd /new && find . -type f | sort | xargs md5sum 2>/dev/null | md5sum | cut -d" " -f1)
  echo "  src=$SRC"
  echo "  dst=$DST"
  [ "$SRC" = "$DST" ] || { echo "  TREES DIFFER"; exit 1; }
  echo "  identical"
' || die "verification failed; $NEW is not a faithful copy"

log "done. Now repoint $WL at $NEW in git, then let ArgoCD roll it."
