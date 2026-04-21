# iSCSI PVC Restore Procedures

## Context

This cluster uses democratic-csi with TrueNAS iSCSI for persistent storage. All 29 PVCs use `truenas-iscsi-ssd` on a single SSD.

A daily CronJob (`pvc-backup` in namespace `backup`) snapshots all SSD PVCs and rsyncs them to NFS on the RAID at `/mnt/RAID/docker/backups-iscsi/`. It also retains the last 7 VolumeSnapshots per PVC for quick restores.

### Backup infrastructure files
- `system/snapshots/backup.yaml` — CronJob, RBAC, backup script
- `system/snapshots/restore.yaml` — Pre-built restore Jobs (one per PVC, 29 total)
- `system/snapshots/snapshot-controller.yaml` — VolumeSnapshot controller
- `system/snapshots/crd-*.yaml` — VolumeSnapshot CRDs
- `system/global-argocd-apps/democratic-csi-iscsi-ssd.yaml` — CSI driver + snapshot class

### Cluster details
- kubectl context: `lamg`
- NAS IP: `192.168.0.29`
- NFS backup path: `/mnt/RAID/docker/backups-iscsi`
- Node selector for jobs: `svccontroller.k3s.cattle.io/lbpool: lamg`
- Scrutiny/InfluxDB have NO ArgoCD app — must be applied manually with `kubectl apply`
- `bootstrap` ArgoCD app has NO auto-sync — needs manual sync trigger

### All 29 iSCSI PVCs (all on `truenas-iscsi-ssd`)

| Namespace | PVC | Size | Deployment File | ArgoCD |
|-----------|-----|------|-----------------|--------|
| agonbar | homarr-appdata | 5Gi | deployments/agonbar/homarr.yaml | yes |
| agonbar | paperless-redis | 1Gi | deployments/agonbar/paperless.yaml | yes |
| agonbar | paperless-data | 5Gi | deployments/agonbar/paperless.yaml | yes |
| dawarich | dawarich-db-data | 5Gi | deployments/dawarich/postgres.yaml | yes |
| games | enshrouded-data | 10Gi | deployments/games/enshrouded.yaml | yes |
| games | factorio-data | 10Gi | deployments/games/factorio.yaml | yes |
| games | palworld-data | 10Gi | deployments/games/palworld.yaml | yes |
| immich | immich-db-data | 5Gi | deployments/immich/postgre.yaml | yes |
| lamg | homeassistant-config | 15Gi | deployments/lamg/homeassistant.yaml | yes |
| lamg | influxdb-data | 5Gi | deployments/scrutiny/influxdb.yaml | **NO** |
| lamg | plex-config | 30Gi | deployments/lamg/plex.yaml | yes |
| lamg | scrutiny-config | 1Gi | deployments/scrutiny/master-web.yaml | **NO** |
| lamg | vscode-config | 5Gi | deployments/lamg/vscode.yaml | yes |
| lamg | zigbee2mqtt-config | 1Gi | deployments/lamg/zigbee2mqtt.yaml | yes |
| piracy | bazarr-config | 5Gi | deployments/piracy/bazarr.yaml | yes |
| piracy | cruncharr-config | 2Gi | deployments/piracy/cruncharr.yml | yes |
| piracy | emulerr-config | 5Gi | deployments/piracy/emulerr.yml | yes |
| piracy | jellyfin-config | 10Gi | deployments/piracy/jellyfin.yml | yes |
| piracy | lidarr-config | 5Gi | deployments/piracy/lidarr.yml | yes |
| piracy | prowlarr-config | 5Gi | deployments/piracy/prowlarr.yml | yes |
| piracy | qbittorrent-config | 5Gi | deployments/piracy/qbittorrent.yml | yes |
| piracy | qui-config | 2Gi | deployments/piracy/qui.yml | yes |
| piracy | radarr-config | 5Gi | deployments/piracy/radarr.yml | yes |
| piracy | seerr-config | 5Gi | deployments/piracy/seerr.yml | yes |
| piracy | slskd-config | 5Gi | deployments/piracy/slskd.yml | yes |
| piracy | sonarr-config | 5Gi | deployments/piracy/sonarr.yml | yes |
| piracy | soularr-config | 1Gi | deployments/piracy/soularr.yml | yes |
| piracy | tachidesk-data | 5Gi | deployments/piracy/tachidesk.yml | yes |
| piracy | transmission-config | 2Gi | deployments/piracy/transmission.yml | yes |

---

## Scenario A: Quick Restore from VolumeSnapshot (SSD is fine, data corrupted)

Use this when the SSD is healthy but a PVC's data is corrupted or you need to roll back to a previous day's state. This restores from the ZFS snapshot on the SSD — instant, no network copy.

Up to 7 daily snapshots are retained per PVC.

### Step 1: Identify available snapshots

```bash
kubectl --context lamg get volumesnapshots -n <NAMESPACE> \
  --sort-by=.metadata.creationTimestamp
```

Pick the snapshot you want to restore from (format: `backup-<PVC_NAME>-<TIMESTAMP>`).

### Step 2: Scale down the affected deployment

For ArgoCD-managed apps, set replicas to 0 in the deployment YAML and push, or:

```bash
kubectl --context lamg scale deployment/<DEPLOYMENT_NAME> -n <NAMESPACE> --replicas=0
```

For scrutiny/influxdb (no ArgoCD):
```bash
kubectl --context lamg scale deployment/scrutiny-master-web -n lamg --replicas=0
kubectl --context lamg scale deployment/influxdb -n lamg --replicas=0
```

Wait for pods to terminate:
```bash
kubectl --context lamg wait --for=delete pod -l app=<APP_LABEL> -n <NAMESPACE> --timeout=120s
```

### Step 3: Delete the corrupted PVC

```bash
kubectl --context lamg delete pvc <PVC_NAME> -n <NAMESPACE>
```

### Step 4: Create a new PVC from the snapshot

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: <PVC_NAME>
  namespace: <NAMESPACE>
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: truenas-iscsi-ssd
  resources:
    requests:
      storage: <SIZE>
  dataSource:
    name: <SNAPSHOT_NAME>
    kind: VolumeSnapshot
    apiGroup: snapshot.storage.k8s.io
```

Apply it:
```bash
kubectl --context lamg apply -f - <<EOF
<paste the YAML above with values filled in>
EOF
```

Wait for it to bind:
```bash
kubectl --context lamg wait pvc/<PVC_NAME> -n <NAMESPACE> \
  --for=jsonpath='{.status.phase}'=Bound --timeout=300s
```

### Step 5: Scale back up

```bash
kubectl --context lamg scale deployment/<DEPLOYMENT_NAME> -n <NAMESPACE> --replicas=1
```

For scrutiny/influxdb (no ArgoCD):
```bash
kubectl --context lamg apply -f deployments/scrutiny/master-web.yaml
kubectl --context lamg apply -f deployments/scrutiny/influxdb.yaml
```

---

## Scenario B: Full Restore from NFS/RAID Backup (SSD died / replaced)

Use this when the SSD failed and was replaced. All 29 iSCSI PVCs on the SSD are gone and need to be recreated from the rsync backups on the RAID.

### Prerequisites

1. New SSD installed in TrueNAS
2. ZFS pool recreated on the new SSD (same pool name as before)
3. democratic-csi driver pods running and healthy:
   ```bash
   kubectl --context lamg get pods -n democratic-csi
   ```
4. StorageClass `truenas-iscsi-ssd` available:
   ```bash
   kubectl --context lamg get storageclass truenas-iscsi-ssd
   ```

### Step 1: Scale down ALL affected deployments

**Recommended**: Set `replicas: 0` in all deployment YAML files in git and push. ArgoCD auto-sync with selfHeal will revert `kubectl scale` commands.

Alternatively, disable auto-sync temporarily:
```bash
for app in agonbar dawarich games immich piracy lamg; do
  kubectl --context lamg -n argocd patch application "$app" \
    -p '{"spec":{"syncPolicy":{"automated":null}}}' --type=merge
done
```

Then scale down all deployments:
```bash
# Agonbar
kubectl --context lamg scale deployment/homarr -n agonbar --replicas=0
kubectl --context lamg scale deployment/paperless -n agonbar --replicas=0

# Dawarich
kubectl --context lamg scale deployment/dawarich-db -n dawarich --replicas=0

# Games
kubectl --context lamg scale deployment/enshrouded -n games --replicas=0
kubectl --context lamg scale deployment/factorio -n games --replicas=0
kubectl --context lamg scale deployment/palworld -n games --replicas=0

# Immich
kubectl --context lamg scale deployment/immich-postgres -n immich --replicas=0

# Lamg (scrutiny/influxdb have no ArgoCD)
kubectl --context lamg scale deployment/homeassistant -n lamg --replicas=0
kubectl --context lamg scale deployment/influxdb -n lamg --replicas=0
kubectl --context lamg scale deployment/plex -n lamg --replicas=0
kubectl --context lamg scale deployment/scrutiny-master-web -n lamg --replicas=0
kubectl --context lamg scale deployment/vscode -n lamg --replicas=0
kubectl --context lamg scale deployment/zigbee2mqtt -n lamg --replicas=0

# Piracy
kubectl --context lamg scale deployment/bazarr -n piracy --replicas=0
kubectl --context lamg scale deployment/cruncharr -n piracy --replicas=0
kubectl --context lamg scale deployment/emulerr -n piracy --replicas=0
kubectl --context lamg scale deployment/jellyfin -n piracy --replicas=0
kubectl --context lamg scale deployment/lidarr -n piracy --replicas=0
kubectl --context lamg scale deployment/prowlarr -n piracy --replicas=0
kubectl --context lamg scale deployment/qbittorrent -n piracy --replicas=0
kubectl --context lamg scale deployment/qui -n piracy --replicas=0
kubectl --context lamg scale deployment/radarr -n piracy --replicas=0
kubectl --context lamg scale deployment/seerr -n piracy --replicas=0
kubectl --context lamg scale deployment/slskd -n piracy --replicas=0
kubectl --context lamg scale deployment/sonarr -n piracy --replicas=0
kubectl --context lamg scale deployment/soularr -n piracy --replicas=0
kubectl --context lamg scale deployment/tachidesk -n piracy --replicas=0
kubectl --context lamg scale deployment/transmission -n piracy --replicas=0
```

### Step 2: Delete old PVCs (if they exist in Pending/Lost state)

```bash
# Check state
kubectl --context lamg get pvc --all-namespaces | grep truenas-iscsi-ssd

# Delete all iSCSI PVCs
kubectl --context lamg delete pvc homarr-appdata paperless-redis paperless-data -n agonbar --ignore-not-found
kubectl --context lamg delete pvc dawarich-db-data -n dawarich --ignore-not-found
kubectl --context lamg delete pvc enshrouded-data factorio-data palworld-data -n games --ignore-not-found
kubectl --context lamg delete pvc immich-db-data -n immich --ignore-not-found
kubectl --context lamg delete pvc homeassistant-config influxdb-data plex-config scrutiny-config vscode-config zigbee2mqtt-config -n lamg --ignore-not-found
kubectl --context lamg delete pvc bazarr-config cruncharr-config emulerr-config jellyfin-config lidarr-config prowlarr-config qbittorrent-config qui-config radarr-config seerr-config slskd-config sonarr-config soularr-config tachidesk-data transmission-config -n piracy --ignore-not-found
```

### Step 3: Recreate empty PVCs

Apply the deployment files which contain PVC definitions. ArgoCD handles this for most apps.

If ArgoCD auto-sync was disabled, re-enable and sync:
```bash
for app in agonbar dawarich games immich piracy lamg; do
  kubectl --context lamg -n argocd patch application "$app" \
    -p '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}' --type=merge
done
```

For scrutiny/influxdb (no ArgoCD):
```bash
kubectl --context lamg apply -f deployments/scrutiny/master-web.yaml
kubectl --context lamg apply -f deployments/scrutiny/influxdb.yaml
```

Wait for all PVCs to bind:
```bash
kubectl --context lamg get pvc --all-namespaces | grep truenas-iscsi-ssd
```

All 29 should show `Bound`. If any are stuck in `Pending`, check democratic-csi logs:
```bash
kubectl --context lamg logs -n democratic-csi -l app.kubernetes.io/name=democratic-csi --tail=50
```

### Step 4: Apply the restore jobs

Apply `system/snapshots/restore.yaml` which contains one Job per PVC (29 total). Each job mounts the new empty PVC and the NFS backup, then rsyncs data back:

```bash
kubectl --context lamg apply -f system/snapshots/restore.yaml
```

Monitor progress:
```bash
# Watch all restore jobs
kubectl --context lamg get jobs --all-namespaces -l app=pvc-restore -w

# Check logs for a specific restore
kubectl --context lamg logs -f -n <NAMESPACE> job/restore-<PVC_NAME>
```

Wait for ALL jobs to complete:
```bash
for ns in agonbar dawarich games immich lamg piracy; do
  kubectl --context lamg wait --for=condition=complete jobs -l app=pvc-restore -n "$ns" --timeout=7200s 2>/dev/null
done
```

**Note**: plex-config (30Gi) has `activeDeadlineSeconds: 7200` and may take longer than others.

### Step 5: Scale back up

If you disabled ArgoCD auto-sync, re-enable it (if not done in step 3):
```bash
for app in agonbar dawarich games immich piracy lamg; do
  kubectl --context lamg -n argocd patch application "$app" \
    -p '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}' --type=merge
done
```

Scale deployments back (or set replicas: 1 in git and push):
```bash
# Agonbar
kubectl --context lamg scale deployment/homarr -n agonbar --replicas=1
kubectl --context lamg scale deployment/paperless -n agonbar --replicas=1

# Dawarich
kubectl --context lamg scale deployment/dawarich-db -n dawarich --replicas=1

# Games
kubectl --context lamg scale deployment/enshrouded -n games --replicas=1
kubectl --context lamg scale deployment/factorio -n games --replicas=1
kubectl --context lamg scale deployment/palworld -n games --replicas=1

# Immich
kubectl --context lamg scale deployment/immich-postgres -n immich --replicas=1

# Lamg
kubectl --context lamg scale deployment/homeassistant -n lamg --replicas=1
kubectl --context lamg scale deployment/influxdb -n lamg --replicas=1
kubectl --context lamg scale deployment/plex -n lamg --replicas=1
kubectl --context lamg scale deployment/scrutiny-master-web -n lamg --replicas=1
kubectl --context lamg scale deployment/vscode -n lamg --replicas=1
kubectl --context lamg scale deployment/zigbee2mqtt -n lamg --replicas=1

# Piracy
kubectl --context lamg scale deployment/bazarr -n piracy --replicas=1
kubectl --context lamg scale deployment/cruncharr -n piracy --replicas=1
kubectl --context lamg scale deployment/emulerr -n piracy --replicas=1
kubectl --context lamg scale deployment/jellyfin -n piracy --replicas=1
kubectl --context lamg scale deployment/lidarr -n piracy --replicas=1
kubectl --context lamg scale deployment/prowlarr -n piracy --replicas=1
kubectl --context lamg scale deployment/qbittorrent -n piracy --replicas=1
kubectl --context lamg scale deployment/qui -n piracy --replicas=1
kubectl --context lamg scale deployment/radarr -n piracy --replicas=1
kubectl --context lamg scale deployment/seerr -n piracy --replicas=1
kubectl --context lamg scale deployment/slskd -n piracy --replicas=1
kubectl --context lamg scale deployment/sonarr -n piracy --replicas=1
kubectl --context lamg scale deployment/soularr -n piracy --replicas=1
kubectl --context lamg scale deployment/tachidesk -n piracy --replicas=1
kubectl --context lamg scale deployment/transmission -n piracy --replicas=1
```

### Step 6: Clean up restore jobs

```bash
kubectl --context lamg delete -f system/snapshots/restore.yaml
```

### Step 7: Verify

```bash
# All pods running
kubectl --context lamg get pods --all-namespaces | grep -v Completed | grep -v kube-system

# All 29 PVCs bound
kubectl --context lamg get pvc --all-namespaces | grep truenas-iscsi-ssd
```

---

## Scenario C: Restore a single PVC from NFS/RAID backup

Use when one specific PVC is lost or corrupted and snapshots are not available.

### Steps

1. Scale down the deployment using the PVC
2. Delete the corrupted PVC
3. Recreate it (apply the deployment file or create PVC manually)
4. Wait for the PVC to bind
5. Run a single restore job:

```bash
kubectl --context lamg apply -f - <<'EOF'
apiVersion: batch/v1
kind: Job
metadata:
  name: restore-<PVC_NAME>
  namespace: <NAMESPACE>
  labels:
    app: pvc-restore
spec:
  backoffLimit: 2
  activeDeadlineSeconds: 3600
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: rsync
          image: alpine:latest
          command:
            - sh
            - -c
            - |
              apk add --no-cache rsync
              rsync -av /backup/<NAMESPACE>/<PVC_NAME>/ /data/
              echo "Restore complete"
          volumeMounts:
            - name: data
              mountPath: /data
            - name: backup
              mountPath: /backup
              readOnly: true
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
            limits:
              cpu: "1"
              memory: 512Mi
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: <PVC_NAME>
        - name: backup
          nfs:
            server: 192.168.0.29
            path: /mnt/RAID/docker/backups-iscsi
      nodeSelector:
        svccontroller.k3s.cattle.io/lbpool: lamg
EOF
```

Replace `<PVC_NAME>` and `<NAMESPACE>` with the actual values.

6. Wait for completion: `kubectl --context lamg wait job/restore-<PVC_NAME> -n <NAMESPACE> --for=condition=complete --timeout=3600s`
7. Delete the job: `kubectl --context lamg delete job restore-<PVC_NAME> -n <NAMESPACE>`
8. Scale deployment back up

---

## NFS paths still on NFS (not affected by SSD failure)

These volumes are NFS-mounted directly and do NOT live on the SSD:

- `/mnt/SSD/torrents/` — shared download dir (piracy apps)
- `/mnt/SSD/torrents/slskd-incomplete` — slskd incomplete downloads
- `/mnt/SSD/torrents/slskd` — slskd completed downloads
- `/mnt/RAID/docker/media/music` — slskd music library
- `/mnt/RAID/docker/paperless/{media,export,consume}` — paperless documents
- All *arr apps media libraries, Plex media, Jellyfin media
- Aya namespace (home assistant, dhcpd, hyperion) — separate NAS
- Immich photos — NFS on RAID
- Dawarich, Syncthing, Netboot.xyz — NFS

**Note**: paths under `/mnt/SSD/` on the NAS ARE on the SSD. If the SSD dies, these NFS shares will also be unavailable. However, these are transient download data (torrents in progress), not critical config data. The completed media lives on `/mnt/RAID/`.
