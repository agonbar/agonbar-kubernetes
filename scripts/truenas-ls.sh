#!/usr/bin/env bash
# List a path on the nas02 TrueNAS over its API.
#
# NFSv4 only shows exported subtrees: mounting an unexported parent like
# /mnt/RAID yields a pseudo-filesystem containing just the paths that lead to
# exports, so `ls` silently hides every other dataset. This asks the NAS
# directly. Credentials are borrowed read-only from the democratic-csi driver
# config, the only place in the cluster holding a TrueNAS API key.
#
# The NAS is LAN-only, so calls are proxied through a long-lived helper pod on a
# lamg node. It is created on first use and left running; `--stop` removes it.
#
# Usage: scripts/truenas-ls.sh /mnt/RAID/adrian [--count]
#        scripts/truenas-ls.sh --stop
set -euo pipefail

CTX="${KUBECTL_CONTEXT:-lamg}"
POD=truenas-api-helper
HOST=192.168.0.29

k() { kubectl --context "$CTX" -n lamg "$@"; }

if [[ "${1:-}" == "--stop" ]]; then
  k delete pod "$POD" --ignore-not-found --wait=false
  exit 0
fi

TARGET="${1:?usage: truenas-ls.sh <path> [--count] | --stop}"
MODE="${2:-}"

if ! k get pod "$POD" -o jsonpath='{.status.phase}' 2>/dev/null | grep -q Running; then
  k delete pod "$POD" --ignore-not-found --wait=true >/dev/null 2>&1
  k run "$POD" --image=busybox:1.36 --restart=Never \
    --overrides='{"spec":{"nodeSelector":{"svccontroller.k3s.cattle.io/lbpool":"lamg"}}}' \
    -- sleep 3600 >/dev/null
  k wait --for=condition=Ready "pod/$POD" --timeout=90s >/dev/null
fi

KEY=$(kubectl --context "$CTX" -n democratic-csi get secret \
  democratic-csi-nfs-ssd-lan-driver-config \
  -o jsonpath='{.data.driver-config-file\.yaml}' \
  | base64 -d | grep -oP 'apiKey:\s*\K\S+')

listdir() {
  k exec "$POD" -- wget -qO- \
    --header="Authorization: Bearer $KEY" \
    --header='Content-Type: application/json' \
    --post-data="{\"path\":\"$1\"}" \
    "http://$HOST/api/v2.0/filesystem/listdir"
}

if [[ "$MODE" == "--count" ]]; then
  # Entry count per subdirectory. ZFS reports it as the directory's size, so
  # this needs no recursion.
  listdir "$TARGET" | jq -r '
    .[] | select(.type=="DIRECTORY") | [(.size|tostring), .name] | @tsv' \
    | sort -rn
else
  listdir "$TARGET" | jq -r '.[] | [.type, (.size|tostring), .name] | @tsv' \
    | sort -k1,1 -k3,3
fi
