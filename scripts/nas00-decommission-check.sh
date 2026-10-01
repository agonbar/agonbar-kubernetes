#!/usr/bin/env bash
# Prove nothing still depends on nas00 before powering it off and pulling its disks.
#
# nas00's Public share moved to nas03 (scripts/nas03-seed-public.sh) and its md1
# RAID5 disks are going into nas02's TrueNAS pool, so the box is being retired.
# Every check below is read-only; a FAIL means something would break on shutdown.
#
#   ./nas00-decommission-check.sh          # run every check, exit 1 if any FAIL
#
# Off the home LAN, point the control-plane SSH at Tailscale:
#   N00_HOST=100.72.0.24 ./nas00-decommission-check.sh
set -uo pipefail

N00_HOST=${N00_HOST:-192.168.0.24}
N00=(ssh -F /dev/null -i "$HOME/.ssh/nas" -o BatchMode=yes -o ConnectTimeout=10 lamg@"$N00_HOST")
KUBECTL=(kubectl --context "${CONTEXT:-lamg}")
NODE=nas00
fails=0

pass(){ printf '  \033[32mPASS\033[0m  %s\n' "$*"; }
fail(){ printf '  \033[31mFAIL\033[0m  %s\n' "$*"; fails=$((fails + 1)); }
head(){ printf '\n== %s\n' "$*"; }

head "pods"
# DaemonSet pods (CSI node plugins, kube-vip, klipper svclb) come back by themselves
# and run on every other node too. Anything else would actually move or die.
others=$("${KUBECTL[@]}" get pods -A --field-selector "spec.nodeName=$NODE" -o json |
  jq -r '.items[] | select((.metadata.ownerReferences // [] | map(.kind) | index("DaemonSet")) | not)
         | "\(.metadata.namespace)/\(.metadata.name)"')
[ -z "$others" ] && pass "solo DaemonSets en $NODE" || fail "pods no-DaemonSet en $NODE:
$others"

head "volumenes"
# A PV whose NFS server or iSCSI portal is nas00 would go read-only cluster-wide.
pvs=$("${KUBECTL[@]}" get pv -o json |
  jq -r --arg n "$NODE" '.items[] | select((tostring) | test("192.168.0.24|100.72.0.24|" + $n))
         | "\(.metadata.name) -> \(.spec.claimRef.namespace)/\(.spec.claimRef.name)"')
[ -z "$pvs" ] && pass "ningun PV apunta a $NODE" || fail "PVs servidos por $NODE:
$pvs"

head "workloads fijados a nas00"
# hostname pins are legacy in this repo, but a live one would sit Pending forever.
pinned=$("${KUBECTL[@]}" get deploy,sts,ds -A -o json |
  jq -r --arg n "$NODE" '.items[]
    | select(.spec.template.spec.nodeSelector["kubernetes.io/hostname"] == $n)
    | select((.status.replicas // 1) > 0)
    | "\(.kind) \(.metadata.namespace)/\(.metadata.name)"')
[ -z "$pinned" ] && pass "nada con replicas vivas fijado a $NODE" || fail "fijado a $NODE:
$pinned"

head "VIP del pool lamg"
# kube-vip ARP-advertises the VIP, so the node that takes over has to sit on the same
# L2 segment -- i.e. it has to be another member of the lbpool, not just any node.
vip=$("${KUBECTL[@]}" get ds -n kube-system kube-vip-lamg \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="address")].value}')
spares=$("${KUBECTL[@]}" get nodes -l svccontroller.k3s.cattle.io/lbpool=lamg -o json |
  jq -r --arg n "$NODE" '.items[] | select(.metadata.name != $n)
         | select([.status.conditions[] | select(.type=="Ready" and .status=="True")] | length > 0)
         | .metadata.name')
n=$(printf '%s\n' "$spares" | grep -c .)
[ "$n" -ge 1 ] && pass "VIP $vip puede saltar a: $(echo $spares)" \
                || fail "VIP $vip se queda sin nodo en el lbpool"

head "datos en el array de nas00"
# The seed only copied Public. Anything else on md1 leaves with the disks.
"${N00[@]}" "findmnt -qno TARGET /mnt/raid-ro" >/dev/null 2>&1 \
  || { echo "  (montando /mnt/raid-ro solo-lectura; no esta en fstab)"; \
       "${N00[@]}" "sudo -n mount -o ro,noload /dev/md1p1 /mnt/raid-ro" >/dev/null 2>&1; }
extra=$("${N00[@]}" "sudo -n ls /mnt/raid-ro" 2>/dev/null | grep -vxE 'Public|lost\+found')
[ -z "$extra" ] && pass "md1 solo tiene Public (ya migrado) y lost+found" \
                || fail "sin copiar fuera de Public:
$extra"

head "clientes de nas00"
# /mnt/RAID is not mounted any more, so the exports and the SMB share point at empty
# directories -- but check for live sessions anyway before yanking the box.
# Sum with awk, not bc: bc is not installed on nas00 nor on work-vm-00, and an empty
# result here would read as zero and pass the check for the wrong reason.
clients=$("${N00[@]}" "sudo -n ss -Htn state established '( sport = :2049 )' 2>/dev/null | wc -l; \
                       sudo -n smbstatus -bp 2>/dev/null | grep -cE '^[0-9]+'" 2>/dev/null |
  awk '{t += $1} END {print (NR ? t : "err")}')
[ "$clients" = 0 ] && pass "sin sesiones NFS ni SMB abiertas" \
                   || fail "sesiones NFS/SMB vivas contra $NODE: $clients"

head "destino"
snap=$(ssh -o BatchMode=yes -o ConnectTimeout=10 nas03 \
  "zfs list -H -o name,used -t snapshot RAID/Public@seed-done" 2>/dev/null)
[ -n "$snap" ] && pass "nas03 tiene el snapshot del seed: $snap" \
               || fail "nas03 no tiene RAID/Public@seed-done"

printf '\n'
[ "$fails" -eq 0 ] && { echo "TODO OK: $NODE se puede apagar."; exit 0; }
echo "$fails comprobacion(es) FAIL: no apagues $NODE todavia."; exit 1
