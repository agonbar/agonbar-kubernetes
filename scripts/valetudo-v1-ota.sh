#!/usr/bin/env bash
# Flash a Dustfab OTA image (Valetudo) onto a factory-fresh Xiaomi Vacuum Gen1
# (rockrobo.vacuum.v1) over its own Wi-Fi AP, then wait for Valetudo to answer.
#
# Meant to run on a host whose Wi-Fi is its only network path: the trap always
# reconnects the Wi-Fi connection that was active at start, so launch it
# detached and read the report afterwards:
#
#   systemd-run --user --unit valetudo-ota \
#     ~/valetudo-v1/valetudo-v1-ota.sh <robot-ssid> <image.pkg> <helper-binary>
#
# Only V1 units made before 2020-03 accept the local OTA; newer ones answer
# unknown_method and stay untouched (then it is UART + vinda, see the vault note
# projects/xiaomi-vacuum-gen1.md). A second-hand unit whose cloud firmware was
# updated answers miIO.ota with an empty payload instead: a full factory reset
# (hold Home + press reset) restores the recovery firmware, which accepted it.
# An empty miIO.info reply does NOT predict a refusal; just try the flash.
set -uo pipefail

SSID=$1 PKG=$2 HELPER=$3
IFACE=${IFACE:-wlp3s0}
KEY=${KEY:-$HOME/.ssh/vacuum/id_ed25519}
DIR=$(dirname "$(readlink -f "$PKG")")
LOG=$DIR/ota-$(date +%Y%m%d-%H%M%S).log
REPORT=$DIR/ota-report.txt

exec >>"$LOG" 2>&1
HOME_CONN=$(nmcli -g GENERAL.CONNECTION dev show "$IFACE")
echo "$(date -Is) start ssid=$SSID pkg=$PKG home_conn=$HOME_CONN"
echo "RUNNING since $(date -Is), log $LOG" >"$REPORT"

result=FAILED
restore() {
  echo "$(date -Is) restoring $HOME_CONN"
  for _ in 1 2 3 4 5; do
    nmcli con up "$HOME_CONN" ifname "$IFACE" && break
    sleep 10
  done
  echo "$result $(date -Is), log $LOG" >"$REPORT"
  kill "$watchdog" 2>/dev/null
}
trap restore EXIT
trap 'result="FAILED: timed out"; exit 1' TERM
# Hard ceiling, whatever hangs: the trap still runs on TERM.
( sleep 1800; kill -TERM $$ ) & watchdog=$!

nmcli dev wifi rescan ifname "$IFACE" 2>/dev/null; sleep 5
nmcli dev wifi connect "$SSID" ifname "$IFACE" || { result="FAILED: cannot join $SSID"; exit 1; }
for _ in $(seq 30); do ip -4 -br addr show "$IFACE" | grep -q '192\.168\.8\.' && break; sleep 2; done
ip -4 -br addr show "$IFACE"

if ! timeout 600 "$HELPER" install-firmware "$PKG"; then
  result="FAILED: helper refused or errored (robot made after 2020-03?)"
  exit 1
fi

echo "$(date -Is) download done, waiting for the robot to install and reboot"
# The robot drops its AP while it reboots; keep rejoining until Valetudo answers.
for _ in $(seq 60); do
  sleep 20
  nmcli -t -f GENERAL.CONNECTION dev show "$IFACE" | grep -q "$SSID" ||
    nmcli dev wifi connect "$SSID" ifname "$IFACE" >/dev/null 2>&1
  if ver=$(curl -sf --max-time 5 http://192.168.8.1/api/v2/valetudo/version); then
    echo "$(date -Is) valetudo up: $ver"
    ssh -i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o ConnectTimeout=10 root@192.168.8.1 'uname -a; df -h /mnt/data' || true
    result="OK: valetudo $ver"
    exit 0
  fi
done
result="FAILED: helper finished but Valetudo never answered on 192.168.8.1"
exit 1
