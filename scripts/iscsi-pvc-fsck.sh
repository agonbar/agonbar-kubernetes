#!/usr/bin/env bash
# Offline e2fsck of a democratic-csi iSCSI PVC.
#
# Use when a pod reports EIO on a file that cannot be read, removed or
# overwritten ("No error information" from ls/rm/stat), and the node's dmesg
# shows lines like:
#   EXT4-fs error (device sdX): ext4_lookup: inode #N: deleted inode referenced
#
# e2fsck must run on an UNMOUNTED device, so this drains the workload, lets the
# CSI detach the LUN, then re-attaches it manually over iSCSI just for the fsck.
# Never run e2fsck on the mounted device: it corrupts the filesystem.
#
# Back up the volume contents before running this. e2fsck drops unrecoverable
# inodes and moves orphans into lost+found.
#
#   ./iscsi-pvc-fsck.sh --context lamg --namespace piracy \
#       --deployment qbittorrent-deployment --pv qbittorrent-config-lan \
#       --node orange-pi5 [--argocd-app piracy] [--check-only]
#
# --node is any cluster node reachable over SSH with sudo and iscsid running.
# It does not have to be the node that held the volume: once the CSI has
# detached it, any node can log in to the target.
set -euo pipefail

CONTEXT="" NS="" DEPLOY="" PV="" NODE="" ARGOCD_APP="" CHECK_ONLY=0 PORTAL=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --context)    CONTEXT="$2"; shift 2 ;;
    --namespace)  NS="$2"; shift 2 ;;
    --deployment) DEPLOY="$2"; shift 2 ;;
    --pv)         PV="$2"; shift 2 ;;
    --node)       NODE="$2"; shift 2 ;;
    --argocd-app) ARGOCD_APP="$2"; shift 2 ;;
    --check-only) CHECK_ONLY=1; shift ;;
    *) echo "argumento desconocido: $1" >&2; exit 2 ;;
  esac
done
for v in CONTEXT NS DEPLOY PV NODE; do
  [[ -n "${!v}" ]] || { echo "falta --${v,,}" >&2; exit 2; }
done

K="kubectl --context $CONTEXT"
say() { printf '\n== %s\n' "$*"; }

# The PV carries everything needed to re-attach the LUN by hand.
IQN=$($K get pv "$PV" -o jsonpath='{.spec.csi.volumeAttributes.iqn}')
PORTAL=$($K get pv "$PV" -o jsonpath='{.spec.csi.volumeAttributes.portal}')
LUN=$($K get pv "$PV" -o jsonpath='{.spec.csi.volumeAttributes.lun}')
[[ -n "$IQN" && -n "$PORTAL" ]] || { echo "el PV $PV no expone iqn/portal iSCSI" >&2; exit 1; }
echo "PV=$PV iqn=$IQN portal=$PORTAL lun=${LUN:-0} nodo=$NODE"

REPLICAS=$($K -n "$NS" get deploy "$DEPLOY" -o jsonpath='{.spec.replicas}')
HAD_AUTOMATED=""

restore() {
  say "restaurando estado"
  $K -n "$NS" scale deploy "$DEPLOY" --replicas="$REPLICAS" >/dev/null || true
  if [[ -n "$HAD_AUTOMATED" ]]; then
    $K -n argocd patch app "$ARGOCD_APP" --type merge \
      -p "{\"spec\":{\"syncPolicy\":{\"automated\":$HAD_AUTOMATED}}}" >/dev/null || true
  fi
}
trap restore EXIT

# ArgoCD selfHeal would scale the deployment straight back up mid-fsck.
if [[ -n "$ARGOCD_APP" ]]; then
  HAD_AUTOMATED=$($K -n argocd get app "$ARGOCD_APP" -o jsonpath='{.spec.syncPolicy.automated}')
  if [[ -n "$HAD_AUTOMATED" ]]; then
    say "desactivando automated sync de ArgoCD ($ARGOCD_APP)"
    $K -n argocd patch app "$ARGOCD_APP" --type merge \
      -p '{"spec":{"syncPolicy":{"automated":null}}}' >/dev/null
  fi
fi

say "escalando $DEPLOY a 0 y esperando el detach"
$K -n "$NS" scale deploy "$DEPLOY" --replicas=0 >/dev/null
SELECTOR=$($K -n "$NS" get deploy "$DEPLOY" \
  -o jsonpath='{range .spec.selector.matchLabels}{@}{end}' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(",".join(f"{k}={v}" for k,v in d.items()))')
for _ in $(seq 1 60); do
  pods=$($K -n "$NS" get pods -l "$SELECTOR" --no-headers 2>/dev/null | wc -l)
  att=$($K get volumeattachment -o jsonpath='{range .items[*]}{.spec.source.persistentVolumeName}{"\n"}{end}' \
        | grep -cx "$PV" || true)
  [[ "$pods" == "0" && "$att" == "0" ]] && break
  sleep 5
done
[[ "${att:-1}" == "0" ]] || { echo "el volumen sigue attached, aborto" >&2; exit 1; }

say "fsck en $NODE"
ssh -o BatchMode=yes "$NODE" "
set -euo pipefail
sudo iscsiadm -m node -o new -T '$IQN' -p '$PORTAL' >/dev/null
sudo iscsiadm -m node -T '$IQN' -p '$PORTAL' --login >/dev/null
cleanup() {
  sudo iscsiadm -m node -T '$IQN' -p '$PORTAL' --logout >/dev/null 2>&1 || true
  sudo iscsiadm -m node -o delete -T '$IQN' -p '$PORTAL' >/dev/null 2>&1 || true
}
trap cleanup EXIT
sleep 4
DEV=\$(readlink -f \"/dev/disk/by-path/ip-${PORTAL}-iscsi-${IQN}-lun-${LUN:-0}\")
echo \"device: \$DEV\"
# Nunca hacer fsck de un device montado: corrompe el filesystem.
mount | grep -q \"^\$DEV \" && { echo 'device montado, aborto'; exit 1; }
sudo tune2fs -l \"\$DEV\" | grep -iE 'filesystem state|FS Error count' || true
if [[ $CHECK_ONLY -eq 1 ]]; then
  sudo e2fsck -fn \"\$DEV\" || true
else
  sudo e2fsck -fy \"\$DEV\" || true
  echo '--- verificación ---'
  sudo e2fsck -fn \"\$DEV\" || true
  sudo tune2fs -l \"\$DEV\" | grep -iE 'filesystem state|FS Error count' || true
fi
"
say "hecho"
