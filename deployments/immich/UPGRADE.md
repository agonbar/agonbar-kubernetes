# Upgrading Immich

ArgoCD auto-syncs this directory from `main`, so an upgrade is an image bump in
`server.yaml` and `machine-learning.yaml`, committed and pushed. Server and
machine-learning always move together, to the same tag.

Last upgrades: v2.7.5 → v3.0.1 (major), v3.0.3 → v3.2.4 on 2026-10-01.

## Before bumping

1. **Read the release notes** for every version you skip
   (`https://github.com/immich-app/immich/releases`), looking for "Breaking
   Changes", Postgres/VectorChord requirements and removed env vars.
2. **Check the mobile apps.** The family updates the Android app on its own, so
   the apps usually run ahead of the server. A newer app against an older server
   fails quietly: in 2026-10 album creation broke and the server logged nothing,
   because the request was rejected by validation. Session rows show what the
   devices run:

   ```bash
   kubectl --context lamg -n immich exec deploy/postgre -- sh -c \
     'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select \"deviceOS\", \"appVersion\", max(\"updatedAt\") from session group by 1,2 order by 3 desc"'
   ```

3. **Take a fresh database dump.** Schema migrations run on the first boot of
   the new server and cannot be undone by reverting the manifests. The database
   lives on `local-path` (`immich-db-data-local`), which has no CSI snapshotter,
   so the only copy is the logical dump made by `immich-postgres-backup`. Run it
   on demand. The job restores every dump into a scratch database, so a
   completed job means the dump is restorable:

   ```bash
   kubectl --context lamg -n immich create job --from=cronjob/immich-postgres-backup immich-pg-preupgrade-$(date +%Y%m%d)
   kubectl --context lamg -n immich wait --for=condition=complete job/immich-pg-preupgrade-$(date +%Y%m%d) --timeout=900s
   kubectl --context lamg -n immich logs job/immich-pg-preupgrade-$(date +%Y%m%d) | grep "restore verified"
   ```

   Note the dump file name from that line. Rollback needs it.

## Upgrade

```bash
sed -i 's|immich-server:vOLD|immich-server:vNEW|' deployments/immich/server.yaml
sed -i 's|immich-machine-learning:vOLD|immich-machine-learning:vNEW|' deployments/immich/machine-learning.yaml
git commit -am "immich: upgrade vOLD -> vNEW" && git push
kubectl --context lamg -n argocd annotate app immich argocd.argoproj.io/refresh=hard --overwrite
```

## Verify

```bash
kubectl --context lamg -n immich rollout status deploy/server --timeout=600s
kubectl --context lamg -n immich rollout status deploy/machine-learning --timeout=600s
kubectl --context lamg -n immich logs deploy/server | grep -E 'Migration|listening on'
curl -s https://immich.adriangonzalezbarbosa.eu/api/server/version
```

Every `Migration "..."` line must say `succeeded`, and the listening line shows
the new version. `AssetGenerateThumbnails` errors about truncated JPEGs or HEIC
"Security limit exceeded" come from specific broken files and predate the
upgrade. They are not a regression.

## Rollback

Reverting the manifests alone is not enough once migrations have run. The
database has to go back to the pre-upgrade dump.

```bash
# 1. revert the image bump; ArgoCD redeploys the old version
git revert --no-edit <upgrade-commit> && git push

# 2. stop everything that writes to the database
kubectl --context lamg -n immich scale deploy/server deploy/machine-learning --replicas=0

# 3. restore the dump into a recreated, empty database
DUMP=immich-<ts>.dump
kubectl --context lamg -n immich run immich-restore --rm -i --restart=Never --image=postgres:14 \
  --overrides='{"spec":{"nodeSelector":{"svccontroller.k3s.cattle.io/lbpool":"lamg"},
    "containers":[{"name":"immich-restore","image":"postgres:14","stdin":true,
      "command":["bash","-c","set -euo pipefail; f=/backups/'"$DUMP"'; psql -d postgres -c \"DROP DATABASE immich\"; psql -d postgres -c \"CREATE DATABASE immich\"; pg_restore --list $f | grep -vE \"^[0-9]+; [0-9]+ [0-9]+ TYPE [^ ]+ _\" > /tmp/toc.list; pg_restore --use-list /tmp/toc.list -d immich --single-transaction --exit-on-error $f; psql -d immich -tAc \"select count(*) from asset\""],
      "env":[{"name":"PGHOST","value":"postgre.immich.svc.cluster.local"},
        {"name":"PGUSER","valueFrom":{"secretKeyRef":{"name":"immich","key":"dbUser"}}},
        {"name":"PGPASSWORD","valueFrom":{"secretKeyRef":{"name":"immich","key":"dbPassword"}}}],
      "volumeMounts":[{"name":"b","mountPath":"/backups"}]}],
    "volumes":[{"name":"b","persistentVolumeClaim":{"claimName":"immich-pg-backups"}}]}}'

# 4. hand the deployments back to ArgoCD (selfHeal restores replicas: 1)
kubectl --context lamg -n argocd annotate app immich argocd.argoproj.io/refresh=hard --overwrite
```

The `grep -v` on the TOC drops the orphaned `_naturalearth_countries` type. The
restore fails on it otherwise. `postgres-backup.yaml` explains why it exists.
The restore pod prints the asset count at the end. It should match the count in
the backup job's log.

Do not downgrade Immich below v1.133.0: the database is VectorChord-only.

Photos and videos live on NFS (`/mnt/RAID/docker/immich/upload`). Upgrades and
rollbacks do not touch them. Only the database needs restoring.
