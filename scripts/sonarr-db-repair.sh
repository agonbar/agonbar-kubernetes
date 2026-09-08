#!/usr/bin/env bash
#
# sonarr-db-repair.sh — recover Sonarr from "database disk image is malformed".
#
# ROOT CAUSE (seen 2026-07-10): the corruption is NOT primarily in SQLite — the
# ext4 filesystem on the iSCSI PVC (truenas-iscsi-ssd-lan / democratic-csi) gets
# a "bad block bitmap checksum" and starts losing writes ("EXT4-fs: This should
# not happen!! Data will be lost"), which then corrupts sonarr.db pages. So a
# plain SQLite `.recover` fails with "disk I/O error" — you must fsck the block
# device FIRST, then rebuild the DB.
#
# The deployment's iscsi-fsck initContainer only fsck's when the FS is mounted
# read-only; a rw-but-corrupt FS (checksum errors) slips past it, hence this.
#
# What the script does (for every mode):
#   1. Suspends ArgoCD auto-sync on app `piracy` (else selfHeal reverts step 2),
#      saving the original policy and restoring it at the end.
#   2. Scales sonarr-deployment to 0 and waits for the RWO volume to detach.
#   3. Launches a privileged hostPID maintenance pod with the PVC + e2fsprogs +
#      sqlite, does the work, then tears down and scales sonarr back up.
#
# Modes:
#   check    integrity_check on the DB (read-only). Does NOT fsck.
#   fsck     e2fsck -f -y the iSCSI device (fixes the filesystem).
#   repair   fsck, THEN rebuild sonarr.db via `.recover` (keeps a .corrupt copy,
#            drops a corrupt logs.db so Sonarr recreates it). <-- the usual fix.
#   restore  fsck, THEN restore sonarr.db from Sonarr's latest backup zip.
#
# Usage:  scripts/sonarr-db-repair.sh {check|fsck|repair|restore}
#
set -euo pipefail

# ---- config -----------------------------------------------------------------
CONTEXT="${SONARR_CONTEXT:-lamg}"
NAMESPACE="${SONARR_NAMESPACE:-piracy}"
DEPLOYMENT="${SONARR_DEPLOYMENT:-sonarr-deployment}"
PVC="${SONARR_PVC:-sonarr-config}"
DB="${SONARR_DB:-sonarr.db}"
ARGOCD_APP="${SONARR_ARGOCD_APP:-piracy}"       # ArgoCD Application managing this
ARGOCD_NS="${SONARR_ARGOCD_NS:-argocd}"
PUID="${SONARR_PUID:-950}"
POD="sonarr-db-repair"
IMAGE="alpine:latest"

K=(kubectl --context "$CONTEXT" -n "$NAMESPACE")
KA=(kubectl --context "$CONTEXT" -n "$ARGOCD_NS")

MODE="${1:-}"
case "$MODE" in check|fsck|repair|restore) ;; *)
  echo "usage: $0 {check|fsck|repair|restore}" >&2; exit 2 ;; esac

log() { printf '\033[1;36m[repair]\033[0m %s\n' "$*"; }
ex()  { "${K[@]}" exec "$POD" -- sh -c "$1"; }

ORIG_SYNCPOLICY=""; SUSPENDED=0; ORIG_REPLICAS=""

restore_argocd() {
  [ "$SUSPENDED" = 1 ] || return 0
  log "Re-enabling ArgoCD auto-sync on $ARGOCD_APP..."
  "${KA[@]}" patch application "$ARGOCD_APP" --type merge \
    -p "{\"spec\":{\"syncPolicy\":${ORIG_SYNCPOLICY}}}" >/dev/null 2>&1 || \
    log "WARN: could not restore syncPolicy — check app $ARGOCD_APP manually!"
}
teardown() {
  "${K[@]}" delete pod "$POD" --ignore-not-found --wait=false >/dev/null 2>&1 || true
}
trap 'teardown; restore_argocd' EXIT

# ---- 1. suspend ArgoCD self-heal -------------------------------------------
if "${KA[@]}" get application "$ARGOCD_APP" >/dev/null 2>&1; then
  ORIG_SYNCPOLICY="$("${KA[@]}" get application "$ARGOCD_APP" -o jsonpath='{.spec.syncPolicy}')"
  log "Suspending ArgoCD auto-sync on $ARGOCD_APP (saved original policy)."
  "${KA[@]}" patch application "$ARGOCD_APP" --type merge \
    -p '{"spec":{"syncPolicy":{"automated":null}}}' >/dev/null
  SUSPENDED=1
else
  log "No ArgoCD app '$ARGOCD_APP' found — assuming no GitOps self-heal."
fi

# ---- 2. stop Sonarr --------------------------------------------------------
ORIG_REPLICAS="$("${K[@]}" get deploy "$DEPLOYMENT" -o jsonpath='{.spec.replicas}')"
log "Scaling $DEPLOYMENT to 0 (was $ORIG_REPLICAS)..."
"${K[@]}" scale deploy "$DEPLOYMENT" --replicas=0
"${K[@]}" wait --for=delete pod -l app=sonarr --timeout=150s >/dev/null 2>&1 || true

# ---- 3. maintenance pod (privileged, hostPID) ------------------------------
log "Launching maintenance pod '$POD'..."
teardown; "${K[@]}" wait --for=delete pod/"$POD" --timeout=60s >/dev/null 2>&1 || true
"${K[@]}" apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata: { name: $POD, labels: { app: sonarr-db-repair } }
spec:
  restartPolicy: Never
  hostPID: true
  nodeSelector: { svccontroller.k3s.cattle.io/lbpool: lamg }
  containers:
    - name: repair
      image: $IMAGE
      securityContext: { privileged: true }
      command: ["sleep", "3600"]
      volumeMounts:
        - { name: config, mountPath: /config }
  volumes:
    - name: config
      persistentVolumeClaim: { claimName: $PVC }
EOF
"${K[@]}" wait --for=condition=Ready pod/"$POD" --timeout=150s
log "Installing tools..."
ex "apk add --no-cache e2fsprogs util-linux sqlite unzip >/dev/null 2>&1"

# ---- helpers run INSIDE the pod --------------------------------------------
# Unmount the iSCSI device from every namespace, e2fsck it, remount /config raw.
do_fsck() {
  ex '
    set -e
    DEV=$(grep " /config " /proc/self/mounts | awk "{print \$1}" | head -1)
    echo "[fsck] device=$DEV"
    # save CSI globalmount so we can restore it for a clean CSI teardown later
    GM=$(nsenter -t 1 -m -- sh -c "grep \"^$DEV \" /proc/mounts | grep globalmount | awk \"{print \\\$2}\"" | head -1)
    echo "$GM" > /tmp/globalmount
    echo "[fsck] globalmount=$GM"
    for MP in $(nsenter -t 1 -m -- sh -c "grep \"^$DEV \" /proc/mounts | awk \"{print \\\$2}\""); do
      nsenter -t 1 -m -- umount "$MP" 2>/dev/null || nsenter -t 1 -m -- umount -l "$MP" 2>/dev/null || true
    done
    umount /config 2>/dev/null || umount -l /config 2>/dev/null || true
    echo "[fsck] running e2fsck -f -y $DEV"
    e2fsck -f -y "$DEV" || true          # exit 1/2 = errors fixed, that is success
    echo "[fsck] re-checking (expect clean)"
    e2fsck -f -y "$DEV"
    mount "$DEV" /config                 # raw remount for DB work
    echo "[fsck] done"
  '
}
# Restore the CSI globalmount so kubelet NodeUnstage is clean on pod delete.
restore_globalmount() {
  ex '
    DEV=$(nsenter -t 1 -m -- sh -c "grep \" /config \" /proc/self/mounts" >/dev/null 2>&1; grep " /config " /proc/self/mounts | awk "{print \$1}" | head -1)
    GM=$(cat /tmp/globalmount 2>/dev/null)
    umount /config 2>/dev/null || umount -l /config 2>/dev/null || true
    [ -n "$GM" ] && nsenter -t 1 -m -- mount "$DEV" "$GM" 2>/dev/null || true
    nsenter -t 1 -m -- sh -c "grep \"$DEV \" /proc/mounts | grep globalmount" || echo "[warn] globalmount not restored"
  '
}

TS="$(date +%Y%m%d-%H%M%S)"
case "$MODE" in
  check)
    log "integrity_check on /config/$DB (no fsck):"
    ex "cd /config && sqlite3 '$DB' 'PRAGMA integrity_check;' | head -20"
    ;;

  fsck)
    log "Filesystem check only:"
    do_fsck
    restore_globalmount
    ;;

  repair)
    log "Filesystem check first:"
    do_fsck
    log "Rebuilding /config/$DB via .recover (backup: ${DB}.corrupt-${TS})..."
    ex "cd /config && set -e && \
      cp -a '$DB' '${DB}.corrupt-${TS}' && rm -f '${DB}.recovered' && \
      sqlite3 '$DB' '.recover' | sqlite3 '${DB}.recovered' && \
      sqlite3 '${DB}.recovered' 'PRAGMA integrity_check;' | head -3 && \
      mv '${DB}.recovered' '$DB' && rm -f '${DB}-wal' '${DB}-shm' && \
      chown ${PUID}:${PUID} '$DB' && echo 'OK: $DB rebuilt'"
    log "Dropping logs.db if corrupt (Sonarr recreates it)..."
    ex "cd /config && [ \"\$(sqlite3 logs.db 'PRAGMA integrity_check;' 2>&1 | head -1)\" = ok ] || rm -f logs.db logs.db-wal logs.db-shm; echo done"
    restore_globalmount
    ;;

  restore)
    log "Filesystem check first:"
    do_fsck
    LATEST="$(ex "ls -1t /config/Backups/scheduled/sonarr_backup_*.zip /config/Backups/manual/sonarr_backup_*.zip 2>/dev/null | head -1")"
    [ -n "$LATEST" ] || { log "No backup zip under /config/Backups. Aborting."; exit 1; }
    log "Restoring $DB from: $LATEST"
    ex "cd /config && set -e && \
      cp -a '$DB' '${DB}.corrupt-${TS}' && \
      unzip -o '$LATEST' '$DB' -d /config >/dev/null && \
      sqlite3 '$DB' 'PRAGMA integrity_check;' | head -3 && \
      rm -f '${DB}-wal' '${DB}-shm' && chown ${PUID}:${PUID} '$DB' && \
      echo 'OK: $DB restored'"
    restore_globalmount
    ;;
esac

# ---- teardown & bring Sonarr back ------------------------------------------
teardown
"${K[@]}" wait --for=delete pod/"$POD" --timeout=90s >/dev/null 2>&1 || true
log "Scaling $DEPLOYMENT back to ${ORIG_REPLICAS:-1}..."
"${K[@]}" scale deploy "$DEPLOYMENT" --replicas="${ORIG_REPLICAS:-1}"
"${K[@]}" rollout status deploy "$DEPLOYMENT" --timeout=180s
restore_argocd; SUSPENDED=0
trap - EXIT
log "Done. Tail logs with: kubectl --context $CONTEXT -n $NAMESPACE logs deploy/$DEPLOYMENT -f"
