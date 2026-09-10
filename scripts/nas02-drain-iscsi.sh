#!/usr/bin/env bash
# Scale down every workload holding an iSCSI PVC on nas02, so the NAS can be
# powered off without the initiators losing their block device mid-write.
#
# An iSCSI LUN that vanishes under a mounted ext4 aborts the journal and
# remounts read-only; NFS clients just block and recover, so only iSCSI needs
# this. Replica counts are saved to STATE so restore puts them back exactly.
#
#   ./nas02-drain-iscsi.sh drain     # before powering nas02 off
#   ./nas02-drain-iscsi.sh restore   # once nas02 is back and the pool is up
#
# KNOWN LIMITATION -- this is not enough on its own. Every ArgoCD Application
# here runs with selfHeal: true, so a plain `kubectl scale` is reverted within
# seconds. Measured on 2026-09-09: of the four workloads below, three were back
# to 1 replica almost immediately and only lamg/influxdb stayed down. To drain
# for real, first disable autosync on the owning Applications, e.g.
#   kubectl patch app -n argocd <app> --type merge \
#     -p '{"spec":{"syncPolicy":{"automated":null}}}'
# and restore it afterwards. Wiring that in is the pending fix.
#
# The workload list is derived from the live iSCSI sessions:
#   ssh nas02 midclt call iscsi.global.sessions
# then matching each csi-pvc-<uid> IQN against the PVC UIDs in the cluster.
set -euo pipefail

STATE="${STATE:-/tmp/nas02-drain-state}"

# ns/deployment for each PVC served over iSCSI by nas02.
WORKLOADS=(
  agonbar/reactive-resume-postgres
  lamg/influxdb
  agonbar/minio
  piracy/romm
)

case "${1:-}" in
  drain)
    : > "$STATE"
    for w in "${WORKLOADS[@]}"; do
      ns="${w%%/*}"; dep="${w##*/}"
      n=$(kubectl get deploy -n "$ns" "$dep" -o jsonpath='{.spec.replicas}' 2>/dev/null) || {
        echo "skipping $w (not found)"; continue; }
      echo "$ns/$dep $n" >> "$STATE"
      kubectl scale -n "$ns" deploy "$dep" --replicas=0
    done
    echo "--- waiting for pods to die (releases the iSCSI mount) ---"
    for w in "${WORKLOADS[@]}"; do
      ns="${w%%/*}"; dep="${w##*/}"
      kubectl wait -n "$ns" --for=delete pod -l app="$dep" --timeout=120s 2>/dev/null || true
    done
    echo "saved to $STATE"
    echo "VERIFY replicas really are 0 before powering off -- ArgoCD selfHeal may have undone this."
    ;;
  restore)
    [[ -f "$STATE" ]] || { echo "no $STATE found"; exit 1; }
    while read -r target n; do
      kubectl scale -n "${target%%/*}" deploy "${target##*/}" --replicas="$n"
    done < "$STATE"
    ;;
  *)
    echo "usage: $0 {drain|restore}" >&2; exit 2 ;;
esac
