#!/usr/bin/env bash
# Stream from Bazzite (Vibepollo) to the docked Steam Deck once per variant and
# print Moonlight's "Global video stats" for each. Run from a workstation with
# the Deck's CEF port forwarded:
#
#   ssh -i ~/.ssh/nas -fN -L 18080:127.0.0.1:8080 deck@192.168.1.218
#   scripts/moonlight-deck-matrix.sh variants.txt > results.tsv
#
# variants.txt, one per line, "label|flatpak run args|moonlight stream args|conf":
#   base||--4K --fps 60 --video-codec PyroWave --bitrate 300000
#   nolsfg|--env=DISABLE_LSFG=1|--4K --fps 60 --video-codec PyroWave --no-vsync
#   pace900||--4K --fps 60 --video-codec PyroWave|pyrowavepacembps=900
# conf is space-separated Moonlight.conf keys without a CLI flag; the file is
# restored afterwards.
#
# Moonlight logs to stderr, which Steam forwards to the user journal; the stats
# block is written when the session ends. Restores the shortcut's launch options.
set -euo pipefail
cd "$(dirname "$0")/.."

APPID=${APPID:-3956900848}   # Moonlight non-Steam shortcut on the Deck
GAMEID=$(( (APPID << 32) | 0x02000000 ))
HOST=${STREAM_HOST:-192.168.1.212}
SECS=${SECS:-45}
DECK=(ssh -n -i ~/.ssh/nas -o BatchMode=yes ${DECK:-deck@192.168.1.218})   # -n: the loop reads variants from stdin
BAZZITE=(ssh -n -i ~/.ssh/nas -o BatchMode=yes bazzite@$HOST)
cef() { scripts/steam-cef-eval.mjs "$1"; }
running() { "${DECK[@]}" 'ps -eo comm | grep -q "^moonlight$"'; }
stop() { "${DECK[@]}" 'for p in $(ps -eo pid,comm | awk "\$2 == \"moonlight\" {print \$1}"); do kill -TERM $p; done' || true
         for _ in $(seq 20); do running || return 0; sleep 1; done; }

CONF='"$HOME/.var/app/com.moonlight_stream.Moonlight/config/Moonlight Game Streaming Project/Moonlight.conf"'
"${DECK[@]}" "cp $CONF /tmp/Moonlight.conf.matrix"
ORIG=$(cef "new Promise(r => SteamClient.Apps.RegisterForAppDetails($APPID, d => r(d.strLaunchOptions)))" | python3 -c 'import json,sys; print(json.load(sys.stdin))')
trap 'stop; "${DECK[@]}" "cp /tmp/Moonlight.conf.matrix $CONF"; cef "SteamClient.Apps.SetShortcutLaunchOptions($APPID, \"$ORIG\")" >/dev/null' EXIT

# Client side from Moonlight's stats; host side from Vibepollo's debug log: wire_ms
# is the time from a frame's first packet to its last, which grows as the pace
# drops toward the bitrate and isn't in Moonlight's stats.
echo -e "label\thost_ms\tnet_drop\tpacing_drop\tdecode_ms\tqueue_ms\trender_ms\trender_fps\tvb_process_ms\tvb_wire_ms\tvb_pacing"

while IFS='|' read -r label fpargs mlargs conf; do
  [[ -z $label || $label == \#* ]] && continue
  stop; sleep 5   # Steam needs a moment after the process exits before it relaunches
  set_conf="cp /tmp/Moonlight.conf.matrix $CONF"
  for kv in $conf; do
    set_conf+="; grep -q '^${kv%%=*}=' $CONF && sed -i 's/^${kv%%=*}=.*/$kv/' $CONF || sed -i '/^\\[General\\]/a $kv' $CONF"
  done
  "${DECK[@]}" "$set_conf"
  cef "SteamClient.Apps.SetShortcutLaunchOptions($APPID, \"run --branch=master --arch=x86_64 $fpargs --command=moonlight com.moonlight_stream.Moonlight stream $HOST Desktop $mlargs --no-quit-after --no-performance-overlay\")" >/dev/null
  since=$("${DECK[@]}" 'date "+%Y-%m-%d %H:%M:%S"')
  for _ in 1 2 3; do
    cef "SteamClient.Apps.RunGame(\"$(printf %u $GAMEID)\", \"\", -1, 100)" >/dev/null
    for _ in $(seq 20); do running && break; sleep 1; done
    running && break
  done
  running || { echo -e "$label\tdid not start"; continue; }
  sleep "$SECS"
  host=$("${BAZZITE[@]}" 'f=$(ls -t ~/.config/vibepollo/logs/*.log | head -1)
    p=$(grep -a "Frame processing latency" $f | tail -1 | grep -oE "[0-9.]+ms$")
    w=$(grep -a "overall network latency" $f | tail -1 | grep -oE "[0-9.]+ms$")
    pace=$(grep -a "PyroWave pacing:" $f | tail -1 | grep -oE "[0-9]+ packets/ms[^,]*")
    printf "%s\t%s\t%s" "${p%ms}" "${w%ms}" "$pace"')
  stop; sleep 2
  client=$("${DECK[@]}" "journalctl --user --since '$since' --no-pager -o cat" |
    awk '/Global video stats/ {on=1; next}
      on && /Host processing latency/ {v=$NF; if (v=="ms") v=$(NF-1); split(v,a,"/"); host=a[3]}
      on && /dropped by your network/ {net=$NF}
      on && /dropped by client pacing/ {pac=$NF}
      on && /Average decoding time/ {dec=$4}
      on && /frame queue delay/ {q=$5}
      on && /Average rendering time/ {r=$(NF-1)}
      on && /Rendering frame rate/ {fps=$4}
      END {printf "%s\t%s\t%s\t%s\t%s\t%s\t%s", host, net, pac, dec, q, r, fps}')
  echo -e "$label\t$client\t$host"
done < "${1:-/dev/stdin}"
