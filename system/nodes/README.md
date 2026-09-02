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

## Why orange-pi5 has a boot.cmd here

k3s v1.36 refuses to start on cgroup v1: the kubelet exits immediately with
`kubelet is configured to not run on a host using cgroup v1`, and the node never
registers. orange-pi5 was pinned to v1 by an explicit
`systemd.unified_cgroup_hierarchy=0` in its U-Boot script, so the 2026-09-01
upgrade left it dead until the old binary was restored.

Fixed 2026-09-02 by flipping that flag to `1` and rebooting. The bootargs are
compiled into `boot.scr`, so editing `boot.cmd` alone does nothing:

```sh
ssh orange-pi5
cd /boot/firmware
sudo cp -a boot.cmd boot.cmd.bak && sudo cp -a boot.scr boot.scr.bak
sudo sed -i 's/systemd.unified_cgroup_hierarchy=0/systemd.unified_cgroup_hierarchy=1/' boot.cmd
sudo mkimage -C none -A arm -T script -d boot.cmd boot.scr
sudo reboot
```

Verify with `stat -fc %T /sys/fs/cgroup`: `cgroup2fs` is correct, `tmpfs` means
still on v1. The `.bak` files are left on the board; if it ever fails to boot,
restoring `boot.scr.bak` from an SD reader is the way back.

All three home nodes now report `cgroup2fs` and run v1.36.4, so the
`k3s-upgrade=disabled` labels have been removed and the plan covers them again.
The work-vm nodes keep the label permanently: they are NixOS and the upgrade job
cannot write to the read-only nix store.
