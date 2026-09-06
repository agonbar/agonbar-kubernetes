#!/usr/bin/env python3
"""Classify every scaled-to-0 Deployment by the storage it needs.

Written on 2026-09-06, after a week of shedding workloads to keep nas01 and
orange-pi5 alive. The question "what can I safely bring back?" came up every
time, and answering it by eye is how you restart the one thing that remounts a
broken iSCSI LUN.

Volumes are not only PVCs. immich/server and dawarich-app mount NFS directly,
with no claim, so a PVC-only scan reports them as stateless and you conclude
they are safe for the wrong reason. Every volume type is inspected here.

Usage:  ./classify-shed-storage.py [kube-context]     (default: lamg)
"""
import json, subprocess, sys

ctx = sys.argv[1] if len(sys.argv) > 1 else "lamg"


def kget(*args):
    r = subprocess.run(["kubectl", "--context", ctx, *args],
                       capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"kubectl failed: {r.stderr.strip()}")
    return r.stdout


sc = {}
for line in kget("get", "pvc", "-A", "--no-headers", "-o",
                 "custom-columns=K:.metadata.namespace,N:.metadata.name,"
                 "S:.spec.storageClassName").splitlines():
    f = line.split()
    if len(f) == 3:
        sc[(f[0], f[1])] = f[2]

rows = []
for d in json.loads(kget("get", "deploy", "-A", "-o", "json"))["items"]:
    if (d["spec"].get("replicas") or 0) != 0:
        continue
    ns, name = d["metadata"]["namespace"], d["metadata"]["name"]
    kinds = []
    for v in d["spec"]["template"]["spec"].get("volumes", []):
        if "persistentVolumeClaim" in v:
            claim = v["persistentVolumeClaim"]["claimName"]
            kinds.append((sc.get((ns, claim), "?"), claim))
        elif "nfs" in v:
            kinds.append(("nfs-direct", v["nfs"]["server"] + ":" + v["nfs"]["path"]))
        elif "hostPath" in v:
            kinds.append(("hostPath", v["hostPath"]["path"]))
        elif not ({"configMap", "secret", "emptyDir", "projected",
                   "downwardAPI"} & set(v)):
            kinds.append((next(k for k in v if k != "name"), ""))

    if any("iscsi" in k for k, _ in kinds):
        verdict = "ISCSI"
    elif kinds:
        verdict = "SAFE-net"
    else:
        verdict = "SAFE"
    rows.append((verdict, ns, name,
                 ", ".join(f"{d}[{k}]" if d else k for k, d in kinds) or "(none)"))

for v, blurb in (("SAFE", "no durable storage at all"),
                 ("SAFE-net", "network storage only, no iSCSI LUN to remount"),
                 ("ISCSI", "holds an iSCSI claim: fsck the volume before starting")):
    sel = sorted(r for r in rows if r[0] == v)
    if not sel:
        continue
    print(f"\n### {v}  ({len(sel)})  {blurb}")
    for _, ns, n, c in sel:
        print(f"  {ns+'/'+n:42} {c}")
