# scripts

Operational tooling for this cluster and the storage it runs on. Each entry
below is something you run against the cluster, its NAS backends or the apps
deployed here, more than once.

One-off migrations, device setup (consoles, TVs, phones, the Proxmox host)
and anything personal do not belong here. CI fails if a file in `scripts/` is
not listed in this README, so adding one is a deliberate decision.

## Storage (democratic-csi iSCSI on TrueNAS)

| Script | What it does |
| --- | --- |
| `repair-iscsi-volume.sh` | Offline e2fsck of an iSCSI volume after an ext4 journal abort. |
| `iscsi-pvc-fsck.sh` | Offline e2fsck of a single democratic-csi PVC. |
| `iscsi-pvc-audit.sh` | Compares the PVCs of a storage class with what the nightly backup actually copied. |
| `find-readonly-mounts.py` | Finds pods whose iSCSI volume silently flipped to read-only. |
| `audit-storage-classes.sh` | Lists live pods that still mount a volume over the tailnet iSCSI portal. |
| `migrate-pvc-class.sh` | Moves a workload's PVC to another StorageClass, data intact. |
| `pvc-copy.sh` | Copies one PVC onto another file by file and proves the copy matches. |
| `nas02-drain-iscsi.sh` | Scales down every workload holding an iSCSI PVC on nas02 before NAS maintenance. |
| `truenas-orphan-zvols.sh` | Lists, and optionally destroys, zvols no PersistentVolume points at. |

## Postgres and backups

| Script | What it does |
| --- | --- |
| `pg-enable-checksums.sh` | Turns on data page checksums for a database initialised without them. |
| `nas02-pg-snapshots.sh` | Hourly ZFS snapshots of the shared Postgres data directory, kept 48 h. |
| `nas03-pgdumps.sh` | nas03 pulls the nightly Postgres dumps off nas02. |
| `nas03-mirror.sh` | nas03 pulls a read-only mirror of nas02's datasets every night. |
| `uptime-backup-monitors.sh` | Uptime Kuma push monitors, so a backup that fails or never runs pages. |

## Cluster

| Script | What it does |
| --- | --- |
| `bump-image.sh` | Bumps one image or chart version through git and checks the workloads stay healthy. |
| `image-updates.sh` | Lists running images that have a newer tag of the same shape in their registry. |
| `audit-pod-requests.sh` | Compares what each pod uses against what it requests. |
| `devbox-smoke.sh` | Smoke test for `deployments/agonbar/devbox.yaml`, run locally with podman. |
| `netbird-exit-setup.sh` | NetBird management side of the internet exits in `deployments/netbird-exit`. |
| `ovh-dns.sh` | Manages records in the cluster's DNS zone through the OVH API. |
| `ovh-dns-token-wizard.sh` | Walks through creating the OVH API token `ovh-dns.sh` needs. |
| `wizard-argocd-github-token.sh` | Walks through creating the GitHub token ArgoCD pulls this repo with. |

## Apps

| Script | What it does |
| --- | --- |
| `strata-build-image.sh` | Builds the Strata LLM server image with BuildKit inside the cluster and pushes it. |
| `strata-gateway` | Source of the gateway in front of Strata (`deployments/strata`). |
| `tdarr-status.sh` | One-shot answer to "why isn't Tdarr transcoding?". |
| `ha-assist.py` | Exposes entities to Home Assistant Assist and queries the conversation agent. |
| `ha-litellm-drift-configmap.sh` | Regenerates the ConfigMap of the `ha-litellm-drift` CronJob. |
| `bookorbit-bootstrap.sh` | Configures a fresh BookOrbit through its API. |
| `wizard-romm-metadata-keys.sh` | Walks through getting the metadata provider keys RomM needs. |
| `cardigann-sim.py` | Runs a Cardigann indexer definition against the live site the way Prowlarr would. |
| `prowlarr-indexer-audit.sh` | Prowlarr indexers: health, live test, 90-day usefulness, error patterns. |
| `servarr-orphan-audit.sh` | Finds enabled indexers and download clients the *arr apps can no longer reach. |
| `sonarr-db-repair.sh` | Recovers Sonarr from "database disk image is malformed". |
| `sonarr-manual-import-preflight.sh` | The checks to run before a manual Sonarr import. |
| `sonarr-nonupgrade-triage.sh` | Triages finished torrents Sonarr never imported. |
| `qbit-pack-repair.sh` | Repairs a season pack qBittorrent marked complete while the NFS quota was full. |
| `qbit-imported-cleanup.sh` | Frees space on the torrents dataset by removing what is already imported. |
