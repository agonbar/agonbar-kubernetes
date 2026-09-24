#!/usr/bin/env bash
# List, and optionally destroy, the iSCSI zvols on nas02 that no PersistentVolume
# in the cluster refers to any more.
#
# Why they exist: democratic-csi only deletes the zvol when the PV's reclaim
# policy is Delete. Every statically bound PV in this repo is Retain -- that is
# what "retained" means, and it is deliberate, since those PVs were hand-written
# to adopt a zvol that already existed. The cost is that removing the claim
# frees nothing on the NAS. The space comes back here or not at all.
#
# A zvol is only reported orphaned when NO PV in the cluster carries its handle,
# in either csi.volumeHandle or the target IQN. Read that as the safety rule it
# is: this script never decides a volume is unused from its name or its age.
#
#   ./truenas-orphan-zvols.sh                    # report, changes nothing
#   ./truenas-orphan-zvols.sh --prune-snapshots  # drop ZFS snapshots k8s forgot
#   ./truenas-orphan-zvols.sh --delete           # destroy them, iSCSI config first
#
# Run --prune-snapshots first when PersistentVolumes are stuck in Released with
# reclaimPolicy Delete. That is democratic-csi failing DeleteVolume with
# "filesystem has dependent snapshots": a ZFS snapshot outlived the
# VolumeSnapshotContent that created it, so the CSI retries forever and the zvol
# never goes. Clearing them lets the controller finish on its own, which is
# better than deleting the PV by hand -- the external-provisioner only drops the
# pv-protection finalizer after a successful delete, so forcing it leaves the
# object wedged in Terminating instead.
#
# The API key comes from the driver config secret in the cluster, so nothing
# here carries a credential. Both iSCSI drivers point at the same parent
# dataset, and the tailnet address answers from anywhere, so one endpoint covers
# the LAN volumes too.
set -euo pipefail

CONTEXT="${CONTEXT:-lamg}"
HOST="${HOST:-100.72.0.41}"
PARENT="${PARENT:-SSD/k8s/iscsi/vols}"
DELETE=0 PRUNE_SNAPS=0
case "${1:-}" in
  --delete)          DELETE=1 ;;
  --prune-snapshots) PRUNE_SNAPS=1 ;;
  "")                ;;
  *) echo "unknown argument: $1" >&2; exit 2 ;;
esac

KEY=$(kubectl --context "$CONTEXT" -n democratic-csi get secret \
        democratic-csi-iscsi-ssd-driver-config \
        -o jsonpath='{.data.driver-config-file\.yaml}' | base64 -d \
      | sed -n 's/^  apiKey: *//p')
[[ -n "$KEY" ]] || { echo "could not read the TrueNAS API key" >&2; exit 1; }

api() { # api <method> <path> [body]
  local m="$1" p="$2" b="${3:-}" out
  if [[ -n "$b" ]]; then
    out=$(curl -sS -m 120 -X "$m" -H "Authorization: Bearer $KEY" \
      -H 'Content-Type: application/json' -d "$b" "http://${HOST}/api/v2.0${p}")
  else
    out=$(curl -sS -m 120 -X "$m" -H "Authorization: Bearer $KEY" "http://${HOST}/api/v2.0${p}")
  fi
  # The API answers a failed delete with 200 and a JSON body carrying the error,
  # so a silent `>/dev/null` reads exactly like success. The first run of this
  # script reported thirteen volumes destroyed and left four standing.
  case "$out" in
    *'"errno"'*) echo "API error on $m $p: $out" >&2; return 1 ;;
  esac
  printf '%s' "$out"
}

# Via temp files, not environment variables. The dataset listing alone is a few
# hundred KB of JSON, and environment blocks count against the same ARG_MAX as
# the argument list: passing it in $DATASET fails with "Argument list too long"
# before python ever starts.
if [[ "$PRUNE_SNAPS" -eq 1 ]]; then
  T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
  api GET "/zfs/snapshot?limit=0" > "$T/zsnaps.json"
  kubectl --context "$CONTEXT" get volumesnapshotcontent -o json > "$T/vsc.json"
  python3 -c '
import json, sys
t = sys.argv[1]
parent = sys.argv[2] + "/"
known = set()
for c in json.load(open(t + "/vsc.json"))["items"]:
    for h in ((c.get("status") or {}).get("snapshotHandle"),
              ((c.get("spec") or {}).get("source") or {}).get("snapshotHandle")):
        if h:
            known.add(h.split("@")[-1])
orphan = [s["name"] for s in json.load(open(t + "/zsnaps.json"))
          if s.get("name", "").startswith(parent) and s["name"].split("@")[-1] not in known]
print("\n".join(orphan))
' "$T" "$PARENT" > "$T/orphans.txt"
  n=$(grep -c . "$T/orphans.txt" || true)
  echo "ZFS snapshots under $PARENT that no VolumeSnapshotContent refers to: $n"
  while read -r snap; do
    [[ -n "$snap" ]] || continue
    if api DELETE "/zfs/snapshot/id/$(printf '%s' "$snap" | sed 's|/|%2F|g; s|@|%40|g')" >/dev/null; then
      echo "  removed $snap"
    else
      echo "  FAILED  $snap"
    fi
  done < "$T/orphans.txt"
  exit 0
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
api GET "/pool/dataset/id/$(printf '%s' "$PARENT" | sed 's|/|%2F|g')" > "$TMP/dataset.json"
kubectl --context "$CONTEXT" get pv -o json > "$TMP/pvs.json"
api GET "/iscsi/extent"        > "$TMP/extents.json"
api GET "/iscsi/target"        > "$TMP/targets.json"
api GET "/iscsi/targetextent"  > "$TMP/targetextents.json"
kubectl --context "$CONTEXT" get volumesnapshotcontent -o json > "$TMP/snapcontents.json"

python3 -c '
import json, sys

tmp = sys.argv[1]
def load(n): return json.load(open("%s/%s.json" % (tmp, n)))
ds = load("dataset")
pvs = load("pvs")["items"]
extents = load("extents")
targets = load("targets")
tes = load("targetextents")
snapcontents = load("snapcontents")["items"]

# A zvol whose snapshots are still alive is NOT free to destroy, and this is not
# theoretical: on 2026-09-24 the thirteen orphans here had 75 VolumeSnapshot
# contents pointing at them, seven per volume, left over from nightly runs of
# PVCs that no longer exist. Worse, nothing was ever going to clean them up --
# retention only runs for volumes still named in the backup list, so removing a
# volume from that list strands its snapshots forever. Delete the snapshots
# first, then come back here.
snapshotted = {}
for c in snapcontents:
    blob = json.dumps(c)
    ref = c["spec"].get("volumeSnapshotRef", {})
    snapshotted.setdefault(blob, ref)

# every handle the cluster still knows about, however it spells it
live = set()
for pv in pvs:
    csi = pv["spec"].get("csi") or {}
    if csi.get("volumeHandle"):
        live.add(csi["volumeHandle"])
    iqn = (csi.get("volumeAttributes") or {}).get("iqn", "")
    if iqn:
        live.add(iqn.split(":")[-1].replace("csi-", "").replace("-k8s", ""))

ext_by_zvol = {}
for e in extents:
    path = e.get("path", "")
    if path.startswith("zvol/"):
        ext_by_zvol[path.split("/")[-1]] = e
tgt_by_name = {t["name"]: t for t in targets}
te_by_ext = {}
for te in tes:
    te_by_ext.setdefault(te["extent"], []).append(te)

plan = []
for c in sorted(ds.get("children", []), key=lambda x: x["name"]):
    handle = c["name"].split("/")[-1]
    if handle in live:
        continue
    used = (c.get("used") or {}).get("parsed", 0) or 0
    snaps = [r for blob, r in snapshotted.items() if handle in blob]
    e = ext_by_zvol.get(handle)
    t = tgt_by_name.get("csi-%s-k8s" % handle)
    plan.append({
        "handle": handle,
        "dataset": c["name"],
        "used": used,
        "extent": e["id"] if e else None,
        "target": t["id"] if t else None,
        "targetextents": [x["id"] for x in te_by_ext.get(e["id"], [])] if e else [],
        "snapshots": ["%s/%s" % (r.get("namespace"), r.get("name")) for r in snaps],
    })
json.dump(plan, open("%s/plan.json" % tmp, "w"))
' "$TMP"

python3 -c '
import json, sys
plan = json.load(open(sys.argv[1] + "/plan.json"))
total = 0
for p in plan:
    total += p["used"]
    print("  %-46s %8.2f GiB  extent=%-5s target=%-5s snapshots=%d" % (
        p["handle"], p["used"] / 1073741824,
        p["extent"] if p["extent"] is not None else "-",
        p["target"] if p["target"] is not None else "-",
        len(p["snapshots"])))
print()
print("%d orphaned zvols, %.1f GiB" % (len(plan), total / 1073741824))
blocked = [p for p in plan if p["snapshots"]]
if blocked:
    n = sum(len(p["snapshots"]) for p in blocked)
    print()
    print("BLOCKED: %d of them still have %d VolumeSnapshots. Delete those first:" % (len(blocked), n))
    for p in blocked:
        print("  kubectl delete volumesnapshot -n %s %s" % tuple(p["snapshots"][0].split("/")))
        if len(p["snapshots"]) > 1:
            print("    ...and %d more for %s" % (len(p["snapshots"]) - 1, p["handle"]))
    sys.exit(3)
' "$TMP"

[[ "$DELETE" -eq 1 ]] || { echo; echo "report only. pass --delete to destroy them."; exit 0; }

echo
echo "deleting..."
python3 -c '
import json, sys
for p in json.load(open(sys.argv[1] + "/plan.json")):
    print("%s %s %s %s" % (p["handle"], p["extent"] or "-", p["target"] or "-",
                           ",".join(str(x) for x in p["targetextents"]) or "-"))
' "$TMP" | while read -r handle extent target tex; do
  # Order matters: the association, then the extent, then the target, and only
  # then the zvol. Destroying the dataset while an extent still points at it
  # leaves TrueNAS with a broken extent that has to be cleaned up by hand.
  if [[ "$tex" != "-" ]]; then
    for id in ${tex//,/ }; do api DELETE "/iscsi/targetextent/id/${id}" >/dev/null; done
  fi
  [[ "$extent" == "-" ]] || api DELETE "/iscsi/extent/id/${extent}" '{"remove": false, "force": true}' >/dev/null
  [[ "$target" == "-" ]] || api DELETE "/iscsi/target/id/${target}" >/dev/null
  # recursive, because a zvol can still carry ZFS snapshots the cluster knows
  # nothing about -- all four survivors of the first run had exactly one. The
  # VolumeSnapshotContent check above is what makes that safe: anything left at
  # this point is an orphan of an orphan.
  if api DELETE "/pool/dataset/id/$(printf '%s' "${PARENT}/${handle}" | sed 's|/|%2F|g')" \
       '{"recursive": true}' >/dev/null; then
    echo "  destroyed ${handle}"
  else
    echo "  FAILED ${handle}"
  fi
done
echo "done"
