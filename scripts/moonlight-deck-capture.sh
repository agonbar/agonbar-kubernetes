#!/usr/bin/env bash
# Start a Moonlight stream on the docked Steam Deck through its Steam shortcut,
# let it run, screenshot the performance overlay, stop it and restore the
# shortcut. Run from a workstation with the Deck's CEF port forwarded:
#
#   ssh -i ~/.ssh/nas -fN -L 18080:127.0.0.1:8080 deck@192.168.1.218
#   scripts/moonlight-deck-capture.sh <out.png> [moonlight stream args...]
#   scripts/moonlight-deck-capture.sh /tmp/h264.png --4K --fps 60 --video-codec H.264
#
# The shortcut must be launched from Steam: gamescope only shows windows of
# apps Steam started. Host and app default to Bazzite's Sunshine "Desktop".
set -euo pipefail
cd "$(dirname "$0")/.."

out=$1; shift
APPID=${APPID:-3956900848}   # Moonlight non-Steam shortcut on the Deck
# RunGame wants the 64-bit gameid of a non-Steam shortcut; the bare appid works
# once and is then ignored without an error.
GAMEID=$(( (APPID << 32) | 0x02000000 ))
HOST=${STREAM_HOST:-192.168.1.212}
WAIT=${WAIT:-30}             # seconds of streaming before the screenshot
DECK=(ssh -i ~/.ssh/nas -o BatchMode=yes ${DECK:-deck@192.168.1.218})
# BRANCH=master runs Nonary's VRR fork (PyroWave decoder), installed beside stable.
RUN="run --branch=${BRANCH:-stable} --arch=x86_64 --command=moonlight com.moonlight_stream.Moonlight"
cef() { scripts/steam-cef-eval.mjs "$1"; }
running() { "${DECK[@]}" 'ps -eo comm | grep -q "^moonlight$"'; }
stop() { "${DECK[@]}" 'for p in $(ps -eo pid,comm | awk "\$2 == \"moonlight\" {print \$1}"); do kill -TERM $p; done' || true; }

# Restore whatever launch options the shortcut had, not a fixed branch.
ORIG=$(cef "new Promise(r => SteamClient.Apps.RegisterForAppDetails($APPID, d => r(d.strLaunchOptions)))" | python3 -c 'import json,sys; print(json.load(sys.stdin))')
trap 'stop; cef "SteamClient.Apps.SetShortcutLaunchOptions($APPID, \"$ORIG\")" >/dev/null' EXIT

stop
for _ in $(seq 20); do running || break; sleep 1; done
sleep 5   # Steam needs a moment after the process exits before it relaunches
cef "SteamClient.Apps.SetShortcutLaunchOptions($APPID, \"$RUN stream $HOST Desktop $* --performance-overlay --no-quit-after\")" >/dev/null
for try in 1 2 3; do
  cef "SteamClient.Apps.RunGame(\"$(printf %u $GAMEID)\", \"\", -1, 100)" >/dev/null
  for _ in $(seq 20); do running && break; sleep 1; done
  running && break
done
running || { echo "moonlight did not start" >&2; exit 1; }
sleep "$WAIT"
"${DECK[@]}" 'rm -f /tmp/ml.png; gamescopectl screenshot /tmp/ml.png; for i in $(seq 10); do [ -s /tmp/ml.png ] && break; sleep 0.5; done'
scp -q -i ~/.ssh/nas "${DECK[-1]}:/tmp/ml.png" "$out"
echo "$out"
