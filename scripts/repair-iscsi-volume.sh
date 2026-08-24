#!/usr/bin/env bash
# Offline e2fsck for democratic-csi iSCSI volumes on the lamg cluster.
#
# Kubelet only runs `fsck -a` at attach, which refuses anything needing manual
# intervention, so a volume that goes "clean with errors" stays that way and keeps
# accumulating. This does the real repair: quiesce, snapshot, unmount, e2fsck -fy.
#
#   ./repair-iscsi-volume.sh <namespace> <deployment/name> <pvc> [pvc...]
#
# Snapshots are left behind on purpose -- delete them once you are happy.
set -euo pipefail
CTX=${CTX:-lamg}
NS=$1; WL=$2; shift 2; PVCS=("$@")
K="kubectl --context $CTX"
log(){ echo "[$(date +%H:%M:%S)] $*"; }
die(){ echo "FATAL: $*" >&2; exit 1; }

APP=$($K -n argocd get app "$NS" -o name >/dev/null 2>&1 && echo "$NS" || echo "")
REPLICAS=$($K -n "$NS" get "$WL" -o jsonpath='{.spec.replicas}')
log "ns=$NS workload=$WL replicas=$REPLICAS pvcs=${PVCS[*]} argocd-app=${APP:-<none>}"

restore(){
  log "restoring: dropping repair pod, replicas=$REPLICAS, autosync"
  # repair pod must be gone (and its volumes detached) before the app comes back
  $K -n "$NS" delete pod "fsck-$NS-${WL##*/}" --ignore-not-found --wait=true >/dev/null 2>&1 || true
  $K -n "$NS" scale "$WL" --replicas="$REPLICAS" >/dev/null 2>&1 || true
  [ -n "$APP" ] && $K -n argocd patch app "$APP" --type=merge \
    -p '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}' >/dev/null 2>&1 || true
}
trap restore EXIT

# 1. stop ArgoCD fighting us, then quiesce
[ -n "$APP" ] && $K -n argocd patch app "$APP" --type=merge \
  -p '{"spec":{"syncPolicy":{"automated":null}}}' >/dev/null
log "scaling $WL to 0"
$K -n "$NS" scale "$WL" --replicas=0 >/dev/null
until [ -z "$($K -n "$NS" get pods -l "$($K -n "$NS" get "$WL" -o jsonpath='{.spec.selector.matchLabels}' \
      | tr -d '{}"' | tr ',' '\n' | paste -sd, -)" --no-headers 2>/dev/null)" ]; do sleep 3; done
log "pods gone; waiting for detach"
for p in "${PVCS[@]}"; do
  VOL=$($K -n "$NS" get pvc "$p" -o jsonpath='{.spec.volumeName}')
  until ! $K get volumeattachment -o jsonpath='{.items[*].spec.source.persistentVolumeName}' 2>/dev/null | tr ' ' '\n' | grep -qx "$VOL"; do sleep 5; done
  log "  detached: $p ($VOL)"
done

# 2. snapshot each PVC using the class that matches its StorageClass
for p in "${PVCS[@]}"; do
  SC=$($K -n "$NS" get pvc "$p" -o jsonpath='{.spec.storageClassName}')
  SNAP="prefsck-$p"
  $K -n "$NS" delete volumesnapshot "$SNAP" --ignore-not-found >/dev/null 2>&1 || true
  cat <<EOF | $K apply -f - >/dev/null
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshot
metadata: {name: $SNAP, namespace: $NS}
spec:
  volumeSnapshotClassName: $SC
  source: {persistentVolumeClaimName: $p}
EOF
  until [ "$($K -n "$NS" get volumesnapshot "$SNAP" -o jsonpath='{.status.readyToUse}' 2>/dev/null)" = true ]; do sleep 4; done
  log "  snapshot ready: $SNAP (class $SC)"
done

# 3. repair pod: mounting the PVCs re-attaches them so the devices exist
POD="fsck-$NS-${WL##*/}"
MOUNTS=""; VOLS=""
i=0; for p in "${PVCS[@]}"; do
  MOUNTS="$MOUNTS
            - {name: v$i, mountPath: /vol/$i}"
  VOLS="$VOLS
        - name: v$i
          persistentVolumeClaim: {claimName: $p}"
  i=$((i+1))
done
$K -n "$NS" delete pod "$POD" --ignore-not-found --wait=true >/dev/null 2>&1 || true
cat <<EOF | $K apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata: {name: $POD, namespace: $NS}
spec:
  restartPolicy: Never
  hostPID: true
  nodeSelector: {svccontroller.k3s.cattle.io/lbpool: lamg}
  containers:
    - name: fsck
      image: alpine:latest
      command: ["sleep", "7200"]
      securityContext: {privileged: true}
      volumeMounts:$MOUNTS
  volumes:$VOLS
EOF
until $K -n "$NS" get pod "$POD" --no-headers 2>/dev/null | grep -q Running; do sleep 4; done
log "repair pod $POD running"
# No package installs: `apk add` depends on the node's network, which on a flapping
# node hangs forever inside kubectl exec (no timeout). alpine's busybox ships nsenter,
# and the k3s host already has e2fsck/tune2fs/findmnt, so borrow those instead.

# 4. unmount everywhere, then fsck
RC=0
i=0; for p in "${PVCS[@]}"; do
  log "repairing $p ..."
  timeout 900 $K -n "$NS" exec "$POD" -- sh -c "
    set -e
    DEV=\$(grep ' /vol/$i ' /proc/mounts | cut -d' ' -f1)
    [ -n \"\$DEV\" ] || { echo 'FATAL: no device for /vol/$i'; exit 1; }
    echo \"  device=\$DEV\"
    for MP in \$(nsenter -t 1 -m -- findmnt -rn -o TARGET --source \$DEV | tac); do
      nsenter -t 1 -m -- umount \"\$MP\" || { echo \"FATAL: cannot umount \$MP\"; exit 1; }
    done
    umount /vol/$i 2>/dev/null || true
    # refuse to fsck anything still mounted anywhere -- this is what corrupts filesystems
    if grep -q \"^\$DEV \" /proc/mounts; then echo 'FATAL: still mounted in container'; exit 1; fi
    if [ -n \"\$(nsenter -t 1 -m -- findmnt -rn -o TARGET --source \$DEV)\" ]; then echo 'FATAL: still mounted on host'; exit 1; fi
    RC=0; OUT=\$(nsenter -t 1 -m -- e2fsck -fy \"\$DEV\" 2>&1) || RC=\$?
    echo \"  e2fsck exit=\$RC (0=clean, 1=errors corrected, >1=trouble)\"
    if [ \$RC -gt 1 ]; then echo \"\$OUT\"; exit 1; fi
    echo \"\$OUT\" | tail -3 | sed 's/^/    /'
    nsenter -t 1 -m -- tune2fs -l \"\$DEV\" | grep -E '^Filesystem state|^FS Error count' | sed 's/^/  /'
  " || { echo "  !! FAILED on $p"; RC=1; }
  i=$((i+1))
done
log "done (rc=$RC)"
exit $RC
