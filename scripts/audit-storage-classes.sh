#!/usr/bin/env bash
# Which live pods still mount a volume over the tailscale iSCSI portal?
#
# On 2026-09-02 every tailnet iSCSI session on orange-pi5 ping-timed-out in the
# same second, recovery gave up after 120s, and three ext4 journals aborted. The
# LAN sessions to the very same TrueNAS box never blinked. Wrapping WireGuard
# around a block device on your own network buys nothing and costs a filesystem.
#
# The migration that followed moved the volumes we knew were broken. This finds
# the ones nobody noticed: a pod on a LAN node whose volume still takes the
# tunnel. Those are the liability. A pod on ovh02 or a work-vm has no LAN route
# to nas02, so the tunnel is its only option and the finding is expected.
#
#   ./audit-storage-classes.sh [context]        # default: lamg
#
# Exits 1 if a LAN-pool node is mounting a tailnet volume, so it can gate CI.

set -euo pipefail
CTX="${1:-lamg}"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

kubectl --context "$CTX" get pv -o json      > "$TMP/pv.json"
kubectl --context "$CTX" get pods -A -o json > "$TMP/pods.json"
kubectl --context "$CTX" get nodes -o json   > "$TMP/nodes.json"

python3 - "$TMP" <<'PY'
import json, sys
d = sys.argv[1]
load = lambda n: json.load(open(f"{d}/{n}.json"))["items"]

# Nodes carrying the lamg lbpool label sit on the same LAN as nas02, so for them
# the tailnet portal is a detour, not a necessity.
lan = {n["metadata"]["name"] for n in load("nodes")
       if n["metadata"]["labels"].get("svccontroller.k3s.cattle.io/lbpool") == "lamg"}

users = {}
for p in load("pods"):
    if p["status"].get("phase") in ("Succeeded", "Failed"):
        continue
    ns, node = p["metadata"]["namespace"], p["spec"].get("nodeName", "-")
    for v in p["spec"].get("volumes", []):
        c = v.get("persistentVolumeClaim")
        if c:
            users.setdefault((ns, c["claimName"]), []).append((p["metadata"]["name"], node))

bad, ok, idle = [], [], []
for pv in load("pv"):
    if pv["spec"].get("storageClassName") != "truenas-iscsi-ssd":
        continue
    cr = pv["spec"].get("claimRef", {})
    key = (cr.get("namespace"), cr.get("name"))
    mounts = users.get(key)
    if not mounts:
        idle.append(key)
        continue
    for pod, node in mounts:
        (bad if node in lan else ok).append((key, pod, node))

if bad:
    print("LIABILITY -- LAN node reaching nas02 over the tunnel for no reason:")
    for (ns, pvc), pod, node in sorted(bad):
        print(f"  {ns}/{pvc}  <- {pod} on {node}")
    print("  Fix: migrate to truenas-iscsi-ssd-lan (a database) or")
    print("       truenas-nfs-ssd-lan (no live SQLite). See scripts/migrate-pvc-class.sh")
    print()

if ok:
    print("Expected -- no LAN route to nas02, so the tunnel is the only path:")
    for (ns, pvc), pod, node in sorted(ok):
        print(f"  {ns}/{pvc}  <- {pod} on {node}")
    print()

print(f"Declared but unmounted ({len(idle)}): rollback PVCs from the migration,")
print("plus anything genuinely orphaned. Nothing writes to these.")
for ns, pvc in sorted(idle):
    print(f"  {ns}/{pvc}")

sys.exit(1 if bad else 0)
PY
