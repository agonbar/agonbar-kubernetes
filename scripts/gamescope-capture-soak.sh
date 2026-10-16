#!/usr/bin/env bash
# Stress a gamescope PipeWire capture client the way that crashes gamescope:
# stop it (SIGTERM) while frames are flowing, over and over. Run ON Bazzite in
# Game Mode with a game in front, e.g.:
#   ssh bazzite@<host> 'bash -s -- 50 ~/.local/bin/gamescope-led-sync --start br --top 73 --right 40 --bottom 73 --left 40' \
#     < scripts/gamescope-capture-soak.sh
#
# Each round starts the client as a transient user unit, waits until it has
# processed frames (its own "N frames/s" log line), waits a random 0-3 s more so
# the stop lands at varied points in gamescope's paint cycle, stops it, and
# checks coredumpctl. Stops at the first gamescope crash.
set -euo pipefail

rounds=$1; shift
unit=gamescope-capture-soak
crashes() { coredumpctl list --no-legend --since "$start" 2>/dev/null | grep -c gamescope || true; }
start=$(date '+%Y-%m-%d %H:%M:%S')

for ((i = 1; i <= rounds; i++)); do
  systemd-run --user --quiet --unit=$unit -p KillSignal=SIGTERM -p SendSIGKILL=no -p TimeoutStopSec=15 "$@"
  t0=$(date '+%H:%M:%S')
  # led-sync prints a stats line every 5 s once frames arrive.
  for ((w = 0; w < 20; w++)); do
    journalctl --user -u $unit --since "$t0" -o cat | grep -q ' [1-9][0-9]*\.[0-9] frames/s' && break
    sleep 1
  done
  fps=$(journalctl --user -u $unit --since "$t0" -o cat | grep -o '[0-9.]* frames/s' | tail -1)
  sleep "$((RANDOM % 4)).$((RANDOM % 10))"
  systemctl --user stop $unit
  sleep 3
  n=$(crashes)
  echo "round $i: ${fps:-no frames}, gamescope crashes so far: $n"
  if (( n > 0 )); then
    coredumpctl list --no-legend --since "$start" | grep gamescope
    exit 1
  fi
done
echo "$rounds rounds, no gamescope crash"
