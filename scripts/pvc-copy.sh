#!/usr/bin/env bash
# Copy one PVC onto another, file by file, and prove the copy matches.
#
# This is the move used every time a volume changes storage class in this
# cluster: declare the replacement, copy while both exist, verify, then retire
# the original. A VolumeSnapshot cannot do it, because a snapshot can only be
# restored into the same CSI driver it came from -- which is exactly the thing
# being changed.
#
# Both PVCs must be unmounted. The script checks rather than trusting you: a
# copy taken from a volume an app is writing to is not a copy of anything.
#
#   ./pvc-copy.sh --namespace games --from factorio-data --to factorio-data-nfs
#
# The pod runs as root so cp -a can preserve ownership, and on the home pool so
# it can reach both the tailscale and the LAN path to nas02.
set -euo pipefail

CONTEXT="${CONTEXT:-lamg}"
NS="" FROM="" TO=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --context)   CONTEXT="$2"; shift 2 ;;
    --namespace) NS="$2"; shift 2 ;;
    --from)      FROM="$2"; shift 2 ;;
    --to)        TO="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
for v in NS FROM TO; do
  [[ -n "${!v}" ]] || { echo "missing --${v,,}" >&2; exit 2; }
done

k() { kubectl --context "$CONTEXT" "$@"; }
POD="pvc-copy-${FROM}"
cleanup() { k -n "$NS" delete pod "$POD" --ignore-not-found --now >/dev/null 2>&1 || true; }

for pvc in "$FROM" "$TO"; do
  k -n "$NS" get pvc "$pvc" >/dev/null
  holder=$(k -n "$NS" get pods -o json \
    | python3 -c '
import json, sys
pvc = sys.argv[1]
for p in json.load(sys.stdin)["items"]:
    if p["status"].get("phase") in ("Succeeded", "Failed"):
        continue
    for v in p["spec"].get("volumes", []):
        c = v.get("persistentVolumeClaim")
        if c and c["claimName"] == pvc:
            print(p["metadata"]["name"])
' "$pvc")
  [[ -z "$holder" ]] || { echo "$NS/$pvc is mounted by $holder, refusing" >&2; exit 1; }
done

cleanup
trap cleanup EXIT
cat <<EOF | k apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: $POD
  namespace: $NS
spec:
  restartPolicy: Never
  containers:
    - name: copy
      image: alpine:latest
      command:
        - sh
        - -c
        - |
          set -e
          # /src/. rather than /src/ so dotfiles come along, and -a so owners,
          # modes and timestamps survive. An app that finds its config owned by
          # root is a migration that technically copied everything and broke
          # anyway.
          cp -a /src/. /dst/
          # lost+found is an ext4 artefact of the volume being retired. It has
          # no business on the NFS side and rmdir only removes it when empty,
          # so a non-empty one from a past fsck stays and gets noticed.
          rmdir /dst/lost+found 2>/dev/null || true
          apk add --no-cache diffutils >/dev/null 2>&1
          diff -r --no-dereference /src /dst \
            --exclude=lost+found > /tmp/diff.txt 2>&1 && echo "diff: identical" \
            || { echo "DIFFERENCES:"; head -30 /tmp/diff.txt; exit 1; }
          echo "files: \$(find /src -type f | wc -l) -> \$(find /dst -type f | wc -l)"
          echo "bytes: \$(du -sb /src | cut -f1) -> \$(du -sb /dst | cut -f1)"
      volumeMounts:
        - { name: src, mountPath: /src, readOnly: true }
        - { name: dst, mountPath: /dst }
  volumes:
    - name: src
      persistentVolumeClaim:
        claimName: $FROM
    - name: dst
      persistentVolumeClaim:
        claimName: $TO
  nodeSelector:
    svccontroller.k3s.cattle.io/lbpool: lamg
EOF

echo "[$NS] $FROM -> $TO"
for _ in $(seq 1 180); do
  phase=$(k -n "$NS" get pod "$POD" -o jsonpath='{.status.phase}' 2>/dev/null || true)
  [[ "$phase" == "Succeeded" || "$phase" == "Failed" ]] && break
  sleep 10
done
k -n "$NS" logs "$POD" 2>&1 | tail -20
[[ "${phase:-}" == "Succeeded" ]] || { echo "copy pod ended in ${phase:-unknown}" >&2; exit 1; }
