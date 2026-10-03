#!/usr/bin/env bash
# Get an adb connection to the Mi MIX 2 wall panel (HA aya satellite).
#
# Android 9 has no persistent wireless debugging: `adb tcpip` lasts until the
# phone reboots. So: try wifi first; if that fails and the phone is on USB,
# re-arm TCP mode over USB and connect again. On work-vm-00 the USB node loses
# its permissions on every re-enumeration, hence the chmod (plain `sudo` in some
# shells resolves to a non-setuid binary, the wrapper is the real one).
#
#   scripts/mix2-adb.sh            # connect, print the device
#   scripts/mix2-adb.sh shell ...  # connect, then run any adb command on it
set -euo pipefail

addr=192.168.1.217:5555
adb() { nix shell nixpkgs#android-tools -c adb "$@"; }

if ! adb connect "$addr" 2>&1 | grep -q "connected to"; then
  for d in /sys/bus/usb/devices/*; do
    [[ "$(cat "$d/idVendor" 2>/dev/null)" == 18d1 ]] || continue
    /run/wrappers/bin/sudo -n chmod 666 \
      "/dev/bus/usb/$(printf %03d "$(cat "$d/busnum")")/$(printf %03d "$(cat "$d/devnum")")"
  done
  adb kill-server >/dev/null 2>&1 || true
  adb -d tcpip 5555 || { echo "phone not reachable on wifi nor USB" >&2; exit 1; }
  sleep 3
  adb connect "$addr" | grep -q "connected to"
fi

if (($#)); then
  adb -s "$addr" "$@"
else
  adb devices -l | grep "$addr"
fi
