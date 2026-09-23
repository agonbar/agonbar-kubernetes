#!/usr/bin/env bash
# Copy the immich Postgres data directory off its iSCSI PVC onto a local-path
# PVC on the node labelled storage/local-db=true.
#
# Why: immich's DB is the last thing in that namespace on a network block
# device. The photo library and the ML model cache are plain NFS mounts, and
# valkey has no disk at all, so this PVC is the only thing that can take an
# ext4 journal abort when the iSCSI portal stalls. It did, on 2026-09-03.
#
# The copy is a file-level copy, not a dump/restore: source and destination run
# the exact same image (ghcr.io/immich-app/postgres:14-vectorchord0.4.3), so the
# on-disk format, the PG major and the VectorChord version are identical. That
# is only safe while the cluster is cleanly shut down, which the script checks
# instead of assuming.
#
# The source PVC is never written to. It stays declared in postgre.yaml as the
# rollback: to go back, point the deployment's claimName at it again.
#
#   ./immich-db-to-local-path.sh            # copy, verify, leave immich down
#
# Afterwards, commit the claimName switch in deployments/immich/postgre.yaml.
# ArgoCD runs this app with selfHeal, so a kubectl edit gets reverted.
set -euo pipefail

CONTEXT="${CONTEXT:-lamg}"
NS="${NS:-immich}"
SRC_PVC="${SRC_PVC:-immich-db-data-lan}"
DST_PVC="${DST_PVC:-immich-db-data-local}"
IMAGE="${IMAGE:-ghcr.io/immich-app/postgres:14-vectorchord0.4.3}"
POD="immich-db-copy"

k() { kubectl --context "$CONTEXT" "$@"; }

# A copy taken while postgres is writing is a corrupt copy. Refuse rather than
# produce one. Only postgre matters for consistency, but server and ML will
# reconnect to a half-migrated DB and write to the old one, so check all three.
echo "--- checking immich is scaled down ---"
for dep in postgre server machine-learning; do
    n=$(k -n "$NS" get deploy "$dep" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo missing)
    [ "$n" = "0" ] || { echo "FATAL: deploy/$dep has replicas=$n, expected 0" >&2; exit 1; }
    echo "ok $dep replicas=0"
done

k -n "$NS" get pvc "$DST_PVC" >/dev/null 2>&1 || {
    echo "FATAL: PVC $DST_PVC does not exist. Commit it in postgre.yaml and let ArgoCD create it." >&2
    exit 1; }

k -n "$NS" delete pod "$POD" --ignore-not-found --now >/dev/null

echo "--- starting copy pod ---"
# root, so cp -a preserves the 950:950 ownership postgres requires on PGDATA.
# nodeSelector drives where local-path carves the volume out: WaitForFirstConsumer
# means this pod, not the deployment, decides which node the data lives on.
cat <<EOF | k apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: $POD
  namespace: $NS
spec:
  restartPolicy: Never
  nodeSelector:
    storage/local-db: "true"
  containers:
    - name: copy
      image: $IMAGE
      command: ["sleep", "1800"]
      volumeMounts:
        - { name: src, mountPath: /src, readOnly: true }
        - { name: dst, mountPath: /dst }
  volumes:
    - name: src
      persistentVolumeClaim:
        claimName: $SRC_PVC
        readOnly: true
    - name: dst
      persistentVolumeClaim:
        claimName: $DST_PVC
EOF
k -n "$NS" wait --for=condition=Ready "pod/$POD" --timeout=300s

k -n "$NS" exec "$POD" -- bash -euo pipefail -c '
    ctl=/usr/lib/postgresql/14/bin/pg_controldata

    state=$($ctl -D /src | sed -n "s/^Database cluster state: *//p")
    echo "source cluster state: $state"
    [ "$state" = "shut down" ] || { echo "FATAL: source is not cleanly shut down"; exit 1; }

    # An interrupted earlier run leaves a partial PGDATA that postgres would try
    # to start from. Only lost+found is expected on a fresh ext4/local-path dir.
    leftovers=$(ls -A /dst | grep -v "^lost+found$" || true)
    [ -z "$leftovers" ] || { echo "FATAL: /dst is not empty:"; echo "$leftovers"; exit 1; }

    echo "--- copying ---"
    cp -a /src/. /dst/
    sync

    echo "--- verifying ---"
    src_ck=$($ctl -D /src | sed -n "s/^Latest checkpoint location: *//p")
    dst_ck=$($ctl -D /dst | sed -n "s/^Latest checkpoint location: *//p")
    echo "checkpoint src=$src_ck dst=$dst_ck"
    [ "$src_ck" = "$dst_ck" ] || { echo "FATAL: checkpoint mismatch"; exit 1; }

    # pg_controldata only reads one file. Diff catches everything else, and is
    # affordable: this database is under 1 GiB.
    diff -r --no-dereference /src /dst && echo "diff: identical"

    owner=$(stat -c "%u:%g" /dst)
    [ "$owner" = "950:950" ] || { echo "FATAL: /dst owned by $owner, expected 950:950"; exit 1; }
    echo "--- copy verified ---"
    du -sh /dst
'

k -n "$NS" delete pod "$POD" --now >/dev/null
echo
echo "Done. The data now lives on local-path on the storage/local-db node."
echo "Next: switch claimName to $DST_PVC in deployments/immich/postgre.yaml,"
echo "set the replicas back to 1, commit and push. ArgoCD does the rest."
