#!/usr/bin/env python3
"""Set the encoding window on Tdarr's own per-node scheduler.

Tdarr stores, per node, a 24-slot `schedule` array ("00-01" .. "23-00") with a
worker count per slot, plus a `scheduleEnabled` flag. While that flag is False
the array is inert and `workerLimits` governs instead, which is why a schedule
can look configured in the UI and still run all day.

Preferred over gating the DaemonSet from Kubernetes: Tdarr treats a closed slot
as a state, so it stops taking new work rather than having its pod deleted
mid-transcode.

  ./set-node-schedule.py --window 2-4                 # dry run, prints a diff
  ./set-node-schedule.py --window 2-4 --apply
  ./set-node-schedule.py --off --apply                # back to 24/7
"""
import argparse, base64, json, subprocess, sys

API = "http://tdarr.piracy.svc.cluster.local:8266/api/v2/cruddb"


def sh(*a):
    r = subprocess.run(a, capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"failed: {' '.join(a)}\n{r.stderr.strip()}")
    return r.stdout


def api(key, pod, payload):
    out = sh("kubectl", "-n", "piracy", "exec", pod, "-c", "tdarr-node", "--",
             "curl", "-s", "-m", "20", "-X", "POST",
             "-H", f"x-api-key: {key}", "-H", "Content-Type: application/json",
             "-d", json.dumps(payload), API)
    return json.loads(out) if out.strip() else None


def slots(window, workers, health):
    """24 hourly slots; only those inside [start,end) get workers."""
    out = []
    for h in range(24):
        live = window and window[0] <= h < window[1]
        out.append({"_id": f"{h:02d}-{(h+1)%24:02d}",
                    "transcodecpu": workers if live else 0,
                    "transcodegpu": 0,
                    "healthcheckcpu": health if live else 0,
                    "healthcheckgpu": 0})
    return out


ap = argparse.ArgumentParser()
g = ap.add_mutually_exclusive_group(required=True)
g.add_argument("--window", help="hours as START-END, e.g. 2-4 for 02:00-04:00")
g.add_argument("--off", action="store_true", help="disable the scheduler (24/7)")
ap.add_argument("--workers", type=int, default=1, help="transcode workers while open")
ap.add_argument("--health", type=int, default=1, help="healthcheck workers while open")
ap.add_argument("--apply", action="store_true", help="write; otherwise dry run")
args = ap.parse_args()

window = None
if args.window:
    a, b = (int(x) for x in args.window.split("-"))
    if not 0 <= a < b <= 24:
        sys.exit("--window must be START-END within 0-24, START < END")
    window = (a, b)

key = base64.b64decode(sh("kubectl", "get", "secret", "-n", "piracy", "tdarr",
                          "-o", "jsonpath={.data.seeded-api-key}")).decode()
pod = sh("kubectl", "get", "pods", "-n", "piracy", "-l", "app=tdarr-node",
         "-o", "jsonpath={.items[0].metadata.name}")

# Only touch nodes that currently exist; Tdarr keeps rows for retired ones.
live = {f"{n}-pod" for n in sh("kubectl", "get", "pods", "-n", "piracy", "-l",
                               "app=tdarr-node", "-o",
                               "jsonpath={range .items[*]}{.spec.nodeName}{'\\n'}{end}").split()}

nodes = api(key, pod, {"data": {"collection": "NodeJSONDB", "mode": "getAll"}})
print(f"{'node':22} {'was':>10}  {'becomes':>10}")
for n in sorted(nodes, key=lambda x: x["_id"]):
    nid = n["_id"]
    if nid not in live:
        print(f"{nid:22} {'(retired)':>10}  {'skipped':>10}")
        continue
    before = ("off" if not n.get("scheduleEnabled")
              else f"{sum(1 for s in n['schedule'] if s['transcodecpu'] > 0)}h/day")
    after = "off (24/7)" if args.off else f"{window[1]-window[0]}h/day"
    print(f"{nid:22} {before:>10}  {after:>10}")
    if not args.apply:
        continue
    obj = dict(n, scheduleEnabled=not args.off)
    if not args.off:
        obj["schedule"] = slots(window, args.workers, args.health)
    api(key, pod, {"data": {"collection": "NodeJSONDB", "mode": "update",
                            "docID": nid, "obj": obj}})

print("\napplied." if args.apply else "\ndry run; re-run with --apply to write.")
