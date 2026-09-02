#!/usr/bin/env bash
# Compare what each pod actually uses against what it requests.
#
# The lamg scheduler keeps overloading the small home nodes because most
# workloads request token amounts (5m CPU / 30Mi) while using orders of
# magnitude more. The scheduler only ever sees the request, so a 2-core box
# looks empty and collects pods until it falls over. This prints the offenders
# so requests get corrected from measurement instead of guesswork.
#
#   ./audit-pod-requests.sh          # whole cluster, biggest memory liars first
#   ./audit-pod-requests.sh nas00    # only pods on that node
set -euo pipefail
export CTX="${CTX:-lamg}"
export NODE_FILTER="${1:-}"
export TOPFILE
TOPFILE=$(mktemp)
export PODFILE
PODFILE=$(mktemp)
trap 'rm -f "$TOPFILE" "$PODFILE"' EXIT

kubectl --context "$CTX" top pod -A --no-headers > "$TOPFILE" 2>/dev/null
kubectl --context "$CTX" get pods -A -o json > "$PODFILE" 2>/dev/null
python3 - <<'PY'
import json, sys, os

node = os.environ.get("NODE_FILTER", "")
usage = {}
for line in open(os.environ["TOPFILE"]):
    p = line.split()
    if len(p) < 4:
        continue
    cpu, mem = p[2], p[3]
    c = int(cpu[:-1]) if cpu.endswith("m") else int(float(cpu) * 1000)
    m = int(mem[:-2]) if mem.endswith("Mi") else 0
    usage[(p[0], p[1])] = (c, m)

def cpu_of(v):
    if not v:
        return 0
    return int(v[:-1]) if v.endswith("m") else int(float(v) * 1000)

def mem_of(v):
    for suf, mult in (("Gi", 1024), ("Mi", 1), ("Ki", 1 / 1024)):
        if v.endswith(suf):
            return int(float(v[:-2]) * mult)
    return 0

rows = []
for pod in json.load(open(os.environ["PODFILE"]))["items"]:
    ns, name = pod["metadata"]["namespace"], pod["metadata"]["name"]
    nd = pod["spec"].get("nodeName", "?")
    if node and nd != node:
        continue
    if (ns, name) not in usage:
        continue
    rc = rm = 0
    for c in pod["spec"].get("containers", []):
        r = c.get("resources", {}).get("requests", {})
        rc += cpu_of(r.get("cpu", ""))
        rm += mem_of(r.get("memory", ""))
    uc, um = usage[(ns, name)]
    # rank by memory overshoot: memory is what actually kills these nodes
    rows.append((um - rm, ns, name, uc, rc, um, rm, nd))

rows.sort(reverse=True)
print(f"{'NAMESPACE/POD':<50}{'CPU uso/req':>15}{'MEM uso/req':>16}{'exceso':>10}  NODO")
for over, ns, name, uc, rc, um, rm, nd in rows[:25]:
    print(f"{ns + '/' + name:<50}{f'{uc}m/{rc}m':>15}{f'{um}Mi/{rm}Mi':>16}{f'{over}Mi':>10}  {nd}")
PY
