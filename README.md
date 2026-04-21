# agonbar-kubernetes

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

GitOps configuration for my personal [K3s](https://k3s.io/) cluster — a
three-site setup where the control plane runs on a cloud VPS and worker nodes
live in two houses connected over a [Tailscale](https://tailscale.com/) /
[Headscale](https://github.com/juanfont/headscale) mesh. [ArgoCD](https://argo-cd.readthedocs.io/)
reconciles everything in this repo onto the cluster.

## Stack

| Component | Technology |
|-----------|------------|
| **Distribution** | K3s (cloud VPS server + SBC workers across two houses) |
| **Mesh networking** | Tailscale clients on every node, Headscale as the control server. `flannel-iface: tailscale0` so all cluster traffic rides the overlay. |
| **Ingress** | [Traefik](https://traefik.io/) with automatic TLS via [Let's Encrypt](https://letsencrypt.org/) |
| **LoadBalancer IPs** | [kube-vip](https://kube-vip.io/) with per-house pools — a Service annotated `svccontroller.k3s.cattle.io/lbpool=aya` picks IPs only from aya-house nodes, never from the VPS |
| **GitOps** | ArgoCD with envsubst CMP sidecar so workload manifests reference `${BASE_DOMAIN}` instead of the real hostname |
| **Secret management** | [Bitnami Sealed Secrets](https://github.com/bitnami-labs/sealed-secrets), cluster-wide scope |
| **Storage** | [democratic-csi](https://github.com/democratic-csi/democratic-csi) → TrueNAS (iSCSI RWO + NFS shared) |
| **Image updates** | [Keel](https://keel.sh/) in poll mode (no webhook) |
| **Cluster upgrades** | [System Upgrade Controller](https://github.com/rancher/system-upgrade-controller) |

## Two-house cluster with a cloud control plane

Workers live in two physical locations (the `lamg` and `aya` houses); the
control plane runs on a cloud VPS. Nodes carry the
`svccontroller.k3s.cattle.io/lbpool` label to tag house membership, and
workloads that need LAN-local NFS or hardware devices pin to their owning
house via `nodeSelector`. This is why some services (Home Assistant, Hyperion,
Pi-hole) are "duplicated" across `lamg/` and `aya/` — one instance per house,
not a mistake.

The VPS never runs LAN-bound workloads; it's deliberately excluded from both
LB pools and carries no house label.

## Domain stays out of Git

An envsubst
[ConfigManagementPlugin](https://argo-cd.readthedocs.io/en/stable/operator-manual/config-management-plugins/)
sidecar on `argocd-repo-server` substitutes `${BASE_DOMAIN}` at manifest-render
time. Every workload Application runs in plugin mode
(`spec.source.plugin.name: envsubst-plugin-v1`), so committed manifests
reference e.g. `jellyfin.${BASE_DOMAIN}` while the real value lives only in an
in-cluster SealedSecret. Same pattern handles ArgoCD's own ingress via a
dedicated `argocd` Application. See
[`system/argocd/cmp-envsubst-plugin-cm.yaml`](system/argocd/cmp-envsubst-plugin-cm.yaml)
and
[`system/argocd/argocd-repo-server-cmp-sidecar-patch.yaml`](system/argocd/argocd-repo-server-cmp-sidecar-patch.yaml)
for the implementation.

Envsubst uses a single-variable allowlist (`envsubst '$BASE_DOMAIN'`) so the
many `${...}` shell references inside initContainer scripts aren't mangled.

## Repository layout

```
.
├── deployments/                 # Application manifests by namespace
│   ├── agonbar/                 # personal services
│   ├── argocd/                  # ArgoCD's own ingress (plugin-rendered)
│   ├── aya/                     # smart home — aya house
│   ├── dawarich/                # location tracking
│   ├── games/                   # dedicated game servers
│   ├── immich/                  # photo management
│   ├── lamg/                    # smart home + media — lamg house
│   ├── matrix/                  # communication
│   ├── piracy/                  # media acquisition/management
│   ├── scrutiny/                # disk S.M.A.R.T. monitoring
│   └── yavoo/                   # decentralized storage node
│
├── system/                      # platform components
│   ├── argocd/                  # install + CMP sidecar + sealed secret
│   ├── global-argocd-apps/      # Application CRs (app-of-apps)
│   ├── traefik/                 # ingress controller config
│   ├── democratic-csi/          # storage driver config
│   ├── kube-vip/                # LoadBalancer IP pools
│   ├── snapshots/ upgrades/ ...
│
├── seal-secrets.sh              # encrypt secrets.yaml → sealedsecrets.yaml
└── unseal-secrets.sh            # recover from the cluster
```

Each namespace directory contains a `namespace.yaml`, per-app manifests, and a
`sealedsecrets.yaml` for encrypted credentials. The plaintext `secrets.yaml`
counterpart is gitignored.

## Namespaces & Applications

### agonbar — Personal Services

| Application | Description |
|-------------|-------------|
| Vaultwarden | Password manager (Bitwarden-compatible) |
| Gitea | Self-hosted Git service |
| Paperless-ngx | Document management with OCR |
| MinIO | S3-compatible object storage |
| Glance | Personal dashboard |
| Uptime Kuma | Service uptime monitoring |
| Web Adrian / Web Amanda | Personal websites |
| Espuma Chat | Chat application |
| TeamSpeak 3 | Voice communication server |
| Factorio | Dedicated game server |
| OpenClaw, Banderillo, Slash | Custom applications |

### aya — Smart Home & Network

| Application | Description |
|-------------|-------------|
| Home Assistant | Home automation platform |
| Pi-hole | DNS-level ad blocking |
| DHCPD | DHCP server |
| Hyperion | Ambient lighting control |

### dawarich — Location Tracking

| Application | Description |
|-------------|-------------|
| Dawarich | Location history tracking |
| PostgreSQL | Primary database |
| Redis | Cache and session store |
| Sidekiq | Background job processor |

### games — Game Servers

| Application | Description |
|-------------|-------------|
| Palworld | Dedicated Palworld server |
| Enshrouded | Dedicated Enshrouded server |

### immich — Photo Management

| Application | Description |
|-------------|-------------|
| Immich Server | Self-hosted photo and video management |
| Machine Learning | Image classification and face recognition |
| PostgreSQL | Metadata database |
| Redis | Cache layer |

### lamg — Smart home & media (lamg house)

| Application | Description |
|-------------|-------------|
| Home Assistant | Home automation hub |
| Plex | Media streaming server |
| Syncthing | Distributed file synchronization |
| Mosquitto (MQTT) | Message broker for IoT devices |
| Zigbee2MQTT + zigbee2mqtt-assistant | Zigbee bridge |
| NetBoot.xyz | Network boot server |
| VS Code Server | Browser-based code editor |
| Generic Device Plugin | Host-device exposure for containers |

### matrix — Communication

| Application | Description |
|-------------|-------------|
| Synapse | Matrix protocol homeserver |
| Element (Riot) | Matrix web client |
| Slack Bridge | Matrix-Slack integration |
| WhatsApp Bridge | Matrix-WhatsApp integration |

### piracy — Media management

| Application | Description |
|-------------|-------------|
| Prowlarr | Indexer manager |
| Sonarr | TV show management |
| Radarr | Movie management |
| Bazarr | Subtitle management |
| Lidarr | Music management |
| qBittorrent / Transmission | Torrent clients |
| Jellyfin | Open-source media server |
| Seerr | Request management frontend |
| Tdarr | Transcoding pipeline |
| Tachidesk | Manga reader server |
| FlareSolverr | Cloudflare bypass proxy |
| slskd / soularr | Soulseek + Lidarr bridge |
| anisub, cruncharr, emulerr, houndarr, qui | Custom/niche media tooling |

### scrutiny — Disk Health Monitoring

| Application | Description |
|-------------|-------------|
| Scrutiny Web | Disk health dashboard |
| Scrutiny Collector | S.M.A.R.T. data collector |
| InfluxDB | Time-series metrics database |

### yavoo — Decentralized Storage

| Application | Description |
|-------------|-------------|
| Storj Node | Decentralized storage node |
| Multinode Dashboard | Multi-node management UI |
| Profit Bot | Earnings calculation bot |

## How the bootstrap works

`system/argocd/` is the only part of this repo applied directly with `kubectl`:

```bash
kubectl apply -k system/argocd/
```

That installs ArgoCD (v2.14.2), the envsubst CMP sidecar patched onto
`argocd-repo-server`, the plugin ConfigMap, and the SealedSecrets used by
both (`argocd-base-domain` for the domain, `ghcr-pull` for the sidecar's
image). After that, a `bootstrap` Application (created once, by hand —
it has to exist before ArgoCD can reconcile its own source) points at
[`system/global-argocd-apps/`](system/global-argocd-apps/) and creates every
other Application from there. Bootstrap syncs manually; everything downstream
is `automated: {prune: true, selfHeal: true}`.

Node bring-up (flashing SD cards, joining Tailscale, registering the k3s
agent) lives in a separate private repo at `agonbar/k3s-provisioning` — kept
out of this tree because it necessarily carries per-node join tokens.

## Scripts

| Script | Description |
|--------|-------------|
| `seal-secrets.sh` | Encrypts plaintext `secrets.yaml` files into `sealedsecrets.yaml` using `kubeseal` |
| `unseal-secrets.sh` | Recovers and decrypts sealed secrets for local inspection |
| `deploy.sh` | Bulk `kubectl apply` across all deployment manifests (pre-ArgoCD fallback; rarely used now) |

## Secret Management

This repository uses [Bitnami Sealed Secrets](https://github.com/bitnami-labs/sealed-secrets) to safely store encrypted credentials in Git.

**Workflow:**

1. Define secrets in a plaintext `secrets.yaml` file (these are `.gitignored` and never committed).
2. Encrypt the secrets using `kubeseal`:
   ```bash
   ./seal-secrets.sh
   ```
3. The resulting `sealedsecrets.yaml` files are safe to commit. The Sealed Secrets controller in the cluster decrypts them at runtime.

To recover plaintext secrets from the cluster:
```bash
./unseal-secrets.sh
```

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
