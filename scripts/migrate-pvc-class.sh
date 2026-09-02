#!/usr/bin/env bash
# Move PVCs to another StorageClass, on the lamg cluster.
#
# A PVC's StorageClass is immutable and the CSI driver has already carved the zvol
# on one portal, so there is no in-place change. This copies into brand new PVCs and
# leaves the originals untouched, which is the whole point: if the new volume turns
# out wrong, repoint the workload at the old claim and nothing was lost.
#
#   ./migrate-pvc-class.sh <namespace> <deployment/name> <new-class> <pvc> [pvc...]
#
# Pass every PVC of the workload that is moving. They are done in one pass because
# they share a pod, and scaling the same deployment down once per volume would make
# the second run read replicas=0 as the number to restore.
#
# New claims are named <pvc>-lan. The manifest edit is NOT done here: on success the
# workload is left at 0 replicas AND ArgoCD autosync is left suspended. Both are
# deliberate. Bring the app back before the cutover and it writes to the old claim,
# which silently staled the copy you just verified.
set -euo pipefail
CTX=${CTX:-lamg}
NS=$1; WL=$2; NEWSC=$3; shift 3; PVCS=("$@")
K="kubectl --context $CTX"
log(){ echo "[$(date +%H:%M:%S)] $*"; }
die(){ echo "FATAL: $*" >&2; exit 1; }

APP=$($K -n argocd get app "$NS" -o name >/dev/null 2>&1 && echo "$NS" || echo "")
REPLICAS=$($K -n "$NS" get "$WL" -o jsonpath='{.spec.replicas}')
[ "$REPLICAS" != "0" ] || die "$WL is already at 0 replicas; refusing to guess what to restore"
log "ns=$NS workload=$WL pvcs=${PVCS[*]} -> $NEWSC argocd-app=${APP:-<none>}"

POD="migrate-$NS-${WL##*/}"
OK=false
restore(){
  $K -n "$NS" delete pod "$POD" --ignore-not-found --wait=true >/dev/null 2>&1 || true
  if $OK; then
    log "COPY OK. $WL left at 0 and autosync left suspended on purpose. Cut over now:"
    echo
    for p in "${PVCS[@]}"; do
      echo "  - add PVC '$p-lan' (storageClassName: $NEWSC) and repoint claimName: $p -> $p-lan"
    done
    echo "  - keep the old PVCs declared in git, or ArgoCD prune deletes your rollback"
    echo "  - commit, push, then re-enable autosync:"
    echo "      kubectl --context $CTX -n argocd patch app ${APP:-<app>} --type=merge \\"
    echo "        -p '{\"spec\":{\"syncPolicy\":{\"automated\":{\"prune\":true,\"selfHeal\":true}}}}'"
    echo
  else
    log "FAILED: restoring replicas=$REPLICAS and autosync; new PVCs left for inspection"
    $K -n "$NS" scale "$WL" --replicas="$REPLICAS" >/dev/null 2>&1 || true
    [ -n "$APP" ] && $K -n argocd patch app "$APP" --type=merge \
      -p '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}' >/dev/null 2>&1 || true
  fi
}
trap restore EXIT

# 1. stop ArgoCD fighting us, then quiesce so the sources stop changing
[ -n "$APP" ] && $K -n argocd patch app "$APP" --type=merge \
  -p '{"spec":{"syncPolicy":{"automated":null}}}' >/dev/null
log "scaling $WL to 0"
$K -n "$NS" scale "$WL" --replicas=0 >/dev/null
until [ -z "$($K -n "$NS" get pods -l "$($K -n "$NS" get "$WL" -o jsonpath='{.spec.selector.matchLabels}' \
      | tr -d '{}"' | tr ',' '\n' | paste -sd, -)" --no-headers 2>/dev/null)" ]; do sleep 3; done
for p in "${PVCS[@]}"; do
  VOL=$($K -n "$NS" get pvc "$p" -o jsonpath='{.spec.volumeName}')
  until ! $K get volumeattachment -o jsonpath='{.items[*].spec.source.persistentVolumeName}' 2>/dev/null \
        | tr ' ' '\n' | grep -qx "$VOL"; do sleep 5; done
  log "  detached: $p"
done

# 2. destination claims, same size as their sources
for p in "${PVCS[@]}"; do
  SIZE=$($K -n "$NS" get pvc "$p" -o jsonpath='{.spec.resources.requests.storage}')
  OLDSC=$($K -n "$NS" get pvc "$p" -o jsonpath='{.spec.storageClassName}')
  [ "$OLDSC" != "$NEWSC" ] || die "$p is already on $NEWSC"
  cat <<EOF | $K apply -f - >/dev/null
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: $p-lan, namespace: $NS}
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: $NEWSC
  resources: {requests: {storage: $SIZE}}
EOF
  log "  created $p-lan ($SIZE)"
done

# 3. copy pod with every source and destination mounted
MOUNTS=""; VOLS=""
i=0; for p in "${PVCS[@]}"; do
  MOUNTS="$MOUNTS
        - {name: s$i, mountPath: /src/$i, readOnly: true}
        - {name: d$i, mountPath: /dst/$i}"
  VOLS="$VOLS
    - name: s$i
      persistentVolumeClaim: {claimName: $p, readOnly: true}
    - name: d$i
      persistentVolumeClaim: {claimName: $p-lan}"
  i=$((i+1))
done
$K -n "$NS" delete pod "$POD" --ignore-not-found --wait=true >/dev/null 2>&1 || true
cat <<EOF | $K apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata: {name: $POD, namespace: $NS}
spec:
  restartPolicy: Never
  nodeSelector: {svccontroller.k3s.cattle.io/lbpool: lamg}
  affinity:
    nodeAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        nodeSelectorTerms:
          - matchExpressions:
              - {key: kubernetes.io/hostname, operator: NotIn, values: [nas00]}
  containers:
    - name: copy
      image: alpine:latest
      command: ["sleep", "7200"]
      volumeMounts:$MOUNTS
  volumes:$VOLS
EOF
until $K -n "$NS" get pod "$POD" --no-headers 2>/dev/null | grep -q Running; do sleep 4; done
log "copy pod running"

# 4. copy, then verify.
#
# rsync, not tar: a 13Gi claim takes an hour at the speed these volumes move, and
# tar restarts from zero on every retry. rsync makes a second pass incremental,
# which is what you want after a failed verification or a stale copy. --delete
# keeps the destination an exact mirror when re-running.
#
# The counters use find and stat because busybox has neither `du --exclude` nor
# `find -printf`. Two traps live here, both learned the hard way on 2026-09-02:
# an earlier version used `du --exclude`, which busybox silently ignores, so both
# sides returned empty, empty equalled empty and the check passed without ever
# measuring anything. Then -print|xargs split Plex's "Application Support" paths
# on the space and stat summed nothing. Hence -print0, and the non-empty guard.
$K -n "$NS" exec "$POD" -- sh -c 'command -v rsync >/dev/null || apk add --no-cache rsync >/dev/null' \
  || die "no rsync in the copy pod and apk could not install it"
i=0; for p in "${PVCS[@]}"; do
  log "copying $p ..."
  timeout 5400 $K -n "$NS" exec "$POD" -- sh -c "
    set -e
    rsync -aHAX --delete --exclude=/lost+found /src/$i/ /dst/$i/
    sync
  " || die "copy of $p failed"
  $K -n "$NS" exec "$POD" -- sh -c "
    cnt(){ find \"\$1\" -mindepth 1 -path \"\$1/lost+found\" -prune -o -print | wc -l; }
    siz(){ find \"\$1\" -mindepth 1 -path \"\$1/lost+found\" -prune -o -type f -print0 \
             | xargs -0 -r stat -c %s | awk '{s+=\$1} END{print s+0}'; }
    SC=\$(cnt /src/$i); DC=\$(cnt /dst/$i); SS=\$(siz /src/$i); DS=\$(siz /dst/$i)
    echo \"  entries: src=\$SC dst=\$DC\"
    echo \"  bytes:   src=\$SS dst=\$DS\"
    [ -n \"\$SC\" ] && [ -n \"\$SS\" ] && [ \"\$SS\" != 0 ] || { echo '  counters came back empty'; exit 1; }
    [ \"\$SC\" = \"\$DC\" ] || { echo '  MISMATCH in entry count'; exit 1; }
    [ \"\$SS\" = \"\$DS\" ] || { echo '  MISMATCH in bytes'; exit 1; }
    echo '  match'
  " || die "verification of $p failed -- $p-lan is not a faithful copy"
  i=$((i+1))
done

OK=true
