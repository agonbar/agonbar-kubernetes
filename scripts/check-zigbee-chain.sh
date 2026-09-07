#!/usr/bin/env bash
# Health of the whole Zigbee path, in the order it breaks.
#
# Written 2026-09-08 while suspending the hourly generic-device-plugin bounce
# (deployments/lamg/generic-device-plugin-restart.yaml) to find out whether it
# is still needed. Run it for a few days; if every line says OK, the CronJob
# was papering over a bug that is gone.
#
# The point of the ordering: people used to diagnose this by unplugging the USB
# stick, because "the lights do not respond" looks identical whether the dongle
# died, the plugin lost it, or the MQTT broker went away. It is almost never the
# dongle.
set -uo pipefail
CTX=${CTX:-lamg}
K="kubectl --context $CTX"
rc=0
ok(){ printf '  OK    %s\n' "$1"; }
bad(){ printf '  FAIL  %s\n' "$1"; rc=1; }

echo "== 1. dongle advertised by the device plugin =="
adv=$($K get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"="}{.status.allocatable.devices/sonoffzigbeedongle}{"\n"}{end}' 2>/dev/null \
      | awk -F= '$2>0 {print $1"("$2")"}' | tr '\n' ' ')
[ -n "$adv" ] && ok "advertised by: $adv" || bad "no node advertises devices/sonoffzigbeedongle"

echo "== 2. zigbee2mqtt pod =="
read -r name ready phase <<<"$($K -n lamg get pods -l app=zigbee2mqtt \
    -o jsonpath='{.items[0].metadata.name} {.items[0].status.containerStatuses[0].ready} {.items[0].status.phase}' 2>/dev/null)"
case "${phase:-}" in
  Running) [ "$ready" = true ] && ok "$name Running, ready" || bad "$name Running but NOT ready" ;;
  "")      bad "no zigbee2mqtt pod at all" ;;
  # This is the state the suspended CronJob existed to prevent.
  *)       bad "$name in $phase (UnexpectedAdmissionError? plugin lost the device)" ;;
esac

echo "== 3. z2m actually connected to the broker =="
# Same check the liveness probe runs: 075B is 1883, state 01 is ESTABLISHED.
if [ -n "${name:-}" ] && $K -n lamg exec "$name" -- \
     awk '$3 ~ /:075B$/ && $4 == "01" {f=1} END {exit !f}' /proc/net/tcp 2>/dev/null; then
  ok "ESTABLISHED socket to 1883"
else
  bad "no established connection to the broker (z2m is deaf even if Running)"
fi

echo "== 4. broker =="
b=$($K -n lamg get pods -l app=mqtt -o jsonpath='{range .items[*]}{.metadata.name}={.status.containerStatuses[0].ready}{"\n"}{end}' 2>/dev/null | grep '=true' | wc -l)
[ "$b" -ge 1 ] && ok "$b ready broker pod(s)" || bad "no ready broker pod"

echo "== 5. anything stuck on admission =="
u=$($K get pods -A --no-headers 2>/dev/null | grep -ci unexpectedadmission || true)
[ "$u" = 0 ] && ok "no UnexpectedAdmissionError anywhere" || bad "$u pod(s) in UnexpectedAdmissionError"

echo
[ $rc = 0 ] && echo "chain healthy" || echo "chain BROKEN -- if 2 or 5 failed, the CronJob is still needed:
  set suspend: false in deployments/lamg/generic-device-plugin-restart.yaml"
exit $rc
