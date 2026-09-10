#!/usr/bin/env python3
"""Compare SMART counters across the disks of a nas02 pool vdev.

Runs ON nas02 (TrueNAS SCALE), where `midclt` works without root but
smartctl does not. Answers the question a FAULTED disk raises: is the
drive dying, is the cable bad, or is it overheating?

Usage (from a workstation):  ssh nas02 python3 - < scripts/nas02-disk-triage.py
"""
import json
import subprocess
import sys

# Attributes that discriminate between the three failure causes.
KEY = {
    9: "PowerOnHrs",
    5: "Realloc",           # dying media
    197: "Pending",         # dying media
    198: "OfflUncorr",      # dying media
    187: "RepUncorr",       # dying media
    188: "CmdTimeout",      # slow I/O / link
    199: "CRC_cable",       # cable or backplane
    190: "AirflowTemp",     # heat
    194: "Temp",            # heat
    193: "LoadCycle",
}
DISKS = sys.argv[1:] or ["sde", "sdf", "sdg", "sdh"]


def attrs(disk):
    out = subprocess.run(
        ["midclt", "call", "disk.smart_attributes", disk],
        capture_output=True, text=True,
    ).stdout
    try:
        return {KEY[a["id"]]: a["raw"]["value"]
                for a in json.loads(out) if a["id"] in KEY}
    except Exception:
        return {"ERROR": out.strip()[:60]}


def model(disk):
    try:
        info = json.loads(subprocess.run(
            ["midclt", "call", "disk.query", json.dumps([["name", "=", disk]])],
            capture_output=True, text=True).stdout)
        return info[0].get("model", "?")[:13]
    except Exception:
        return "?"


rows = {d: attrs(d) for d in DISKS}
w = 15
print("attribute".ljust(16) + "".join(d.rjust(w) for d in DISKS))
print("".ljust(16) + "".join(model(d).rjust(w) for d in DISKS))
print("-" * (16 + w * len(DISKS)))
for name in KEY.values():
    print(name.ljust(16) + "".join(str(rows[d].get(name, "-")).rjust(w) for d in DISKS))
