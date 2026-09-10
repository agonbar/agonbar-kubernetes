#!/usr/bin/env python3
"""Find pods whose iSCSI volume has silently flipped to read-only.

When an iSCSI target disappears mid-write, ext4 aborts the journal and remounts
ro. The pod keeps running, Kubernetes keeps calling it healthy, and the app
fails however it fails. On 2026-09-10 jellyfin had crashlooped 352 times over
two days with "Read-only file system : '/config/log/.jellyfin-log'" and nothing
pointed at the cause; tdarr was sitting there with its 127MB SQLite database on
a ro mount, running, reported healthy, unable to write a byte.

The kubelet's `fsck -a` at attach does not save you: it refuses anything needing
manual intervention, so a volume that needs a real repair mounts anyway and goes
ro at the first write.

Checking each claim's OWN mountPath matters. A first version of this grepped
/proc/mounts for any ro entry and reported influxdb as broken, because
configmaps and secrets are always mounted ro. Resolve volume -> mountPath ->
that line of /proc/mounts, and nothing else.

  ./find-readonly-mounts.py [kube-context]
"""
import json, subprocess, sys

ctx = sys.argv[1] if len(sys.argv) > 1 else "lamg"


def k(*args, check=True):
    r = subprocess.run(["kubectl", "--context", ctx, *args],
                       capture_output=True, text=True)
    if check and r.returncode:
        sys.exit(f"kubectl failed: {r.stderr.strip()}")
    return r.stdout


iscsi = {f'{p["metadata"]["namespace"]}/{p["metadata"]["name"]}'
         for p in json.loads(k("get", "pvc", "-A", "-o", "json"))["items"]
         if "iscsi" in (p["spec"].get("storageClassName") or "")}
print(f"iSCSI PVCs in the cluster: {len(iscsi)}\n")

rows = []
for pod in json.loads(k("get", "pods", "-A", "--field-selector",
                        "status.phase=Running", "-o", "json"))["items"]:
    ns, name = pod["metadata"]["namespace"], pod["metadata"]["name"]
    spec = pod["spec"]

    # volume name -> claim, keeping only claims on an iSCSI class
    claims = {v["name"]: v["persistentVolumeClaim"]["claimName"]
              for v in spec.get("volumes", [])
              if v.get("persistentVolumeClaim")
              and f'{ns}/{v["persistentVolumeClaim"]["claimName"]}' in iscsi}
    if not claims:
        continue

    for cont in spec.get("containers", []):
        targets = {m["mountPath"]: claims[m["name"]]
                   for m in cont.get("volumeMounts", []) if m["name"] in claims}
        if not targets:
            continue
        mounts = k("-n", ns, "exec", name, "-c", cont["name"], "--",
                   "cat", "/proc/mounts", check=False)
        if not mounts:
            for mp, claim in targets.items():
                rows.append((f"{ns}/{name}", claim, "(no exec)"))
            continue
        # field 2 is the mountpoint, field 4 the option list
        opts = {f[1]: f[3].split(",") for f in
                (l.split() for l in mounts.splitlines()) if len(f) > 3}
        for mp, claim in targets.items():
            o = opts.get(mp)
            if o is None:
                rows.append((f"{ns}/{name}", claim, f"not mounted at {mp}"))
            elif "ro" in o:
                rows.append((f"{ns}/{name}", claim, f"READ-ONLY at {mp}"))
            else:
                rows.append((f"{ns}/{name}", claim, "rw ok"))

# A claim mounted by two containers of the same pod is one volume, not two.
rows = sorted(set(rows), key=lambda r: (not r[2].startswith("READ-ONLY"), r[0]))
print(f'{"POD":48} {"CLAIM":24} STATE')
for p, c, s in rows:
    print(f"{p:48} {c:24} {s}")

broken = sum(1 for r in rows if r[2].startswith("READ-ONLY"))
print()
if broken:
    print(f"{broken} read-only volume(s). Repair each with:\n"
          "  ./scripts/repair-iscsi-volume.sh <ns> deploy/<name> <pvc>")
else:
    print("no read-only iSCSI volumes among running pods")
