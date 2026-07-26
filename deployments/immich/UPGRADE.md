# Immich upgrade v2.7.5 → v3.0.1

Immich v3.0.0 is a **major** release. This runbook covers the upgrade for this GitOps
(ArgoCD auto-sync) deployment.

## Context / risk assessment for THIS deploy

| Breaking change in v3 | Impact here |
|-----------------------|-------------|
| Drops `pgvecto.rs`; VectorChord mandatory | ✅ **Already on VectorChord** (`postgres:14-vectorchord0.4.3`) — no DB extension migration needed |
| API breaking changes → v2 mobile apps stop working | ⚠️ **Every family member must update the Immich mobile app to v3** or it will fail against the server |
| Deprecated env vars removed | ✅ Cleaned up (`TYPESENSE_API_KEY`, `REVERSE_GEOCODING_PRECISION` were already dead/ignored) |
| ML: requires numpy 2.4, removed deprecated envs | ✅ Handled inside the ML image |
| Duration now in ms, star rating ≥1, OAuth secure-by-default, various endpoint removals | Affects third-party API integrations only — none used here |

Schema migrations run automatically on first v3 server boot. **They are not
auto-reversible → take a DB snapshot before syncing.**

Postgres image is left at `14-vectorchord0.4.3` (v3-compatible). Do not downgrade
Immich below v1.133.0 after this point.

## Pre-flight — snapshot the DB (do NOT skip)

The daily backup CronJob snapshots `immich/immich-db-data` at 04:00. Take a fresh
on-demand snapshot right before the upgrade so rollback is a few minutes, not a day, old:

```bash
kubectl --context lamg apply -f - <<'EOF'
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshot
metadata:
  name: immich-db-preupgrade-v3
  namespace: immich
spec:
  volumeSnapshotClassName: truenas-iscsi-ssd
  source:
    persistentVolumeClaimName: immich-db-data
EOF

kubectl --context lamg wait volumesnapshot/immich-db-preupgrade-v3 -n immich \
  --for=jsonpath='{.status.readyToUse}'=true --timeout=300s
```

## Execute

The image bump + env cleanup is already staged in `server.yaml` and
`machine-learning.yaml`. ArgoCD tracks `HEAD` with auto-sync, so committing +
pushing to `main` deploys it:

```bash
git add deployments/immich/
git commit -m "immich: upgrade v2.7.5 -> v3.0.1 (major), drop dead typesense/geocoding envs"
git push
```

Watch the rollout:

```bash
kubectl --context lamg -n immich rollout status deploy/server --timeout=600s
kubectl --context lamg -n immich rollout status deploy/machine-learning --timeout=600s
kubectl --context lamg -n immich logs deploy/server -f   # confirm migrations run clean
```

## Verify

- `https://immich.${BASE_DOMAIN}` loads and login works
- Server → Administration → check version shows `v3.0.1`, no migration errors in logs
- A photo thumbnail / timeline loads (VectorChord search still works)
- Update the mobile app on each device to v3 and confirm sync

## Rollback

Config-only revert is **not enough** if schema migrations already ran — the DB must
also be restored from the pre-upgrade snapshot.

```bash
# 1. revert the manifests
git revert --no-edit <upgrade-commit-sha> && git push

# 2. scale down server + postgres
kubectl --context lamg -n immich scale deploy/server deploy/machine-learning deploy/postgre --replicas=0
kubectl --context lamg -n immich wait --for=delete pod -l app=postgre --timeout=120s

# 3. restore the DB PVC from the pre-upgrade snapshot
kubectl --context lamg -n immich delete pvc immich-db-data
kubectl --context lamg apply -f - <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: immich-db-data
  namespace: immich
spec:
  accessModes: ["ReadWriteOnce"]
  storageClassName: truenas-iscsi-ssd
  resources:
    requests:
      storage: 5Gi
  dataSource:
    name: immich-db-preupgrade-v3
    kind: VolumeSnapshot
    apiGroup: snapshot.storage.k8s.io
EOF
kubectl --context lamg -n immich wait pvc/immich-db-data \
  --for=jsonpath='{.status.phase}'=Bound --timeout=300s

# 4. ArgoCD selfHeal scales the reverted (v2.7.5) deployments back up
```

Photos/originals live on NFS (`/mnt/RAID/docker/immich/upload`) and are untouched by
the upgrade — only the Postgres PVC needs rollback.
