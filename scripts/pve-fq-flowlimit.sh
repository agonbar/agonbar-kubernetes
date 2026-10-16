#!/bin/bash
# Raise the fq per-flow packet limit on proxmox-agb00's physical NICs. Run this
# ON the host. Idempotent.
#
#   ssh pve-agb00 bash -s < scripts/pve-fq-flowlimit.sh
#
# /etc/sysctl.d/99-kernel-bbr.conf makes fq the default qdisc, and fq drops
# anything past 100 queued packets in one flow. Steam Remote Play with PyroWave
# sends each 4K frame as one burst of 400+ UDP packets from the Bazzite VM, so
# fq dropped ~25% of them on the USB NIC (flows_plimit in `tc -s qdisc`) and
# 77% of frames arrived incomplete (2026-10-14). A udev rule, not a post-up,
# because the USB NIC gets replugged and re-enslaved without ifup.
#
# Find which hop drops packets: scripts/stream-hop-counters.sh

set -euo pipefail

NICS="enx00e04c6817f0|enp8s0"
QDISC="fq limit 20000 flow_limit 4000"
RULE=/etc/udev/rules.d/99-fq-flowlimit.rules

# 99- so NAME is already set by 80-net-setup-link.
echo "ACTION==\"add\", SUBSYSTEM==\"net\", NAME==\"$NICS\", RUN+=\"/usr/sbin/tc qdisc replace dev \$name root $QDISC\"" > "$RULE"
udevadm control --reload

for nic in ${NICS//|/ }; do
  tc qdisc replace dev "$nic" root $QDISC
  tc qdisc show dev "$nic" root
done
