#!/usr/bin/env bash
# Turn on Postgres data page checksums for a database that was initialised
# without them, offline.
#
# Why it matters here: without checksums a corrupt page is read back as if it
# were fine, and this cluster has a history of exactly that failure -- iSCSI
# LUNs vanishing under a mounted ext4 and aborting the journal. Checksums do not
# prevent corruption, they make it show up as an error instead of as wrong data.
#
# POSTGRES_INITDB_ARGS="--data-checksums" in a manifest only applies the first
# time a data directory is created. Any cluster initialised before that env was
# added still has them off, and nothing warns about it. Check with:
#   pg_controldata -D <PGDATA> | grep checksum
#
# The cluster must be cleanly shut down: pg_checksums refuses otherwise, and the
# script verifies it rather than trusting the scale-down. ArgoCD selfHeal is
# suspended for the duration, because a `kubectl scale` against an auto-synced
# Application is undone within seconds.
#
#   ./pg-enable-checksums.sh                      # immich
#   NS=dawarich DEPLOY=postgres PVC=... DEPS="app sidekiq" ./pg-enable-checksums.sh
#
# Take a backup first. For immich:
#   kubectl -n immich create job pre --from=cronjob/immich-postgres-backup
set -euo pipefail

CONTEXT="${CONTEXT:-lamg}"
NS="${NS:-immich}"
APP="${APP:-$NS}"                    # ArgoCD Application name
DEPLOY="${DEPLOY:-postgre}"          # the postgres deployment
DEPS="${DEPS:-server machine-learning redis}"   # what to stop alongside it
PVC="${PVC:-immich-db-data-local}"
IMAGE="${IMAGE:-ghcr.io/immich-app/postgres:14-vectorchord0.4.3}"
BINDIR="${BINDIR:-/usr/lib/postgresql/14/bin}"
UID_GID="${UID_GID:-950}"
POD="pg-checksums-$NS"

k() { kubectl --context "$CONTEXT" "$@"; }

restore_autosync() {
    echo "--- re-enabling ArgoCD auto-sync on $APP ---"
    k -n argocd patch app "$APP" --type merge \
        -p '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}' >/dev/null
}

echo "--- suspending ArgoCD auto-sync on $APP ---"
k -n argocd patch app "$APP" --type merge \
    -p '{"spec":{"syncPolicy":{"automated":null}}}' >/dev/null
trap restore_autosync EXIT

echo "--- stopping $DEPLOY $DEPS ---"
for d in $DEPLOY $DEPS; do k -n "$NS" scale deploy "$d" --replicas=0 >/dev/null; done
for d in $DEPLOY $DEPS; do
    k -n "$NS" rollout status deploy "$d" --timeout=180s >/dev/null
done
echo "stopped"

k -n "$NS" delete pod "$POD" --ignore-not-found --now >/dev/null

# runAsUser matches the data directory owner: the postgres tools will not touch
# a PGDATA they do not own, and running them as root is not an option either.
cat <<EOF | k apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: $POD
  namespace: $NS
spec:
  restartPolicy: Never
  securityContext:
    runAsUser: $UID_GID
    fsGroup: $UID_GID
  containers:
    - name: checksums
      image: $IMAGE
      command: ["sleep", "900"]
      volumeMounts:
        - { name: data, mountPath: /data }
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: $PVC
EOF
k -n "$NS" wait --for=condition=Ready "pod/$POD" --timeout=300s >/dev/null

k -n "$NS" exec "$POD" -- bash -euo pipefail -c "
    ctl=$BINDIR/pg_controldata
    sums=$BINDIR/pg_checksums

    state=\$(\$ctl -D /data | sed -n 's/^Database cluster state: *//p')
    echo \"cluster state: \$state\"
    [ \"\$state\" = 'shut down' ] || { echo 'FATAL: not cleanly shut down'; exit 1; }

    before=\$(\$ctl -D /data | sed -n 's/^Data page checksum version: *//p')
    echo \"checksum version before: \$before\"
    if [ \"\$before\" != '0' ]; then echo 'already enabled, nothing to do'; exit 0; fi

    \$sums --enable --progress -D /data

    after=\$(\$ctl -D /data | sed -n 's/^Data page checksum version: *//p')
    echo \"checksum version after: \$after\"
    [ \"\$after\" = '1' ] || { echo 'FATAL: still off'; exit 1; }

    # --check re-reads every page and verifies what was just written.
    \$sums --check -D /data
"

k -n "$NS" delete pod "$POD" --now >/dev/null
echo "--- checksums enabled; ArgoCD will scale everything back from git ---"
