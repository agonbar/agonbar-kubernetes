#!/usr/bin/env bash
# Snapshot packet counters on each hop of the Bazzite VM -> agb00 -> Steam Deck
# Remote Play path, twice, N seconds apart. Run from a workstation during a stream:
#
#   scripts/stream-hop-counters.sh [seconds]
#
# Compare deltas: VM ens18 tx ~ agb00 tap104i0 rx ~ agb00 USB NIC tx ~ Deck NIC rx.
# A gap between tap104i0 rx and the USB NIC tx is a qdisc drop on agb00, see
# scripts/pve-fq-flowlimit.sh.
N=${1:-10}
K=(-i ~/.ssh/nas -o BatchMode=yes)
vm='s=$(awk "/^Udp:/{getline; print \$4, \$6, \$7}" /proc/net/snmp); n=/sys/class/net/ens18/statistics; echo "vm udp_inerr/rcvbuf/sndbuf=$s ens18_tx_pk=$(cat $n/tx_packets) tx_drop=$(cat $n/tx_dropped)"'
hv='for n in tap104i0 enx00e04c6817f0; do s=/sys/class/net/$n/statistics; echo "agb00 $n rx_pk=$(cat $s/rx_packets) tx_pk=$(cat $s/tx_packets) tx_drop=$(cat $s/tx_dropped)"; done'
dk='s=$(awk "/^Udp:/{getline; print \$2, \$4, \$6}" /proc/net/snmp); n=/sys/class/net/enp4s0f3u1u4/statistics; echo "deck udp_in/inerr/rcvbuf=$s nic_rx_pk=$(cat $n/rx_packets) rx_drop=$(cat $n/rx_dropped)"'
snap() {
  ssh "${K[@]}" bazzite@192.168.1.212 "$vm" & ssh "${K[@]}" root@192.168.1.35 "$hv" & ssh "${K[@]}" deck@192.168.1.236 "$dk" & wait
}
snap; sleep "$N"; echo "--- +${N}s"; snap
