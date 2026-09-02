# Per-node k3s config

These are **not** applied by ArgoCD. They live at `/etc/rancher/k3s/config.yaml`
on the node itself and are copied here so the values are reviewable and
recoverable after a k3s reinstall, which does not preserve them.

To apply one:

```sh
scp system/nodes/nas00-k3s-config.yaml nas00:/tmp/config.yaml
ssh nas00 'sudo mkdir -p /etc/rancher/k3s && sudo mv /tmp/config.yaml /etc/rancher/k3s/config.yaml \
  && sudo systemctl restart k3s-agent'
```

## Why nas00 has one

Its Allocatable was the full 3.3Gi, so the scheduler treated a 2-core box with
five democratic-csi DaemonSets already resident as empty. Pods accumulated until
load hit 39 and the CSI driver was OOM-killed, which took every volume on the
node down with it (2026-09-01). The reservations bring Allocatable to ~1.2Gi,
which is what is genuinely free, and the eviction thresholds make the node shed
load instead of dying.

The numbers come from measurement, not taste. Re-derive them with
`scripts/audit-pod-requests.sh nas00` after cordoning the node, so only the
DaemonSets are running.

## The underlying problem this works around

Most workloads in this repo request `cpu: 5m` / `memory: 30Mi` while using far
more — palworld measured 950m/1015Mi against 5m/30Mi, and the democratic-csi
DaemonSets request nothing at all while using ~140Mi each on every node. The
scheduler only sees requests, so it will keep overloading whichever node looks
emptiest. Reserving capacity per node treats the symptom; correcting the
requests is the fix. `scripts/audit-pod-requests.sh` ranks the worst offenders.
