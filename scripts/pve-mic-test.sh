#!/bin/bash
# Test the analog inputs of proxmox-agb00's onboard audio (ALC1220). Run this ON
# the host: 0d:00.4 is not passed through to any VM, so the pink and blue jacks
# can only be recorded from here.
#
#   ssh pve-agb00 bash -s [seconds] < scripts/pve-mic-test.sh
#
# Talk into the mic while it records, 5 seconds per input by default. It prints
# jack detection, then peak and RMS per input. An input with nothing plugged in
# stays at the noise floor on both. The recordings stay in /tmp/mic-test-*.wav.
#
# Needs alsa-utils on the host. The mixer goes back to how it was on exit.
#
# This opens hw:0,0 directly, so it fails with "Device or resource busy" while a
# client is recording through the PipeWire share (scripts/pve-mic-share.sh).

set -euo pipefail

SECS=${1:-5}
CARD=0
STATE=$(mktemp)

alsactl --file "$STATE" store $CARD
trap 'alsactl --file "$STATE" restore $CARD; rm -f "$STATE"' EXIT

level() {
    python3 - "$1" <<'EOF'
import array, math, sys, wave
w = wave.open(sys.argv[1])
s = array.array("h", w.readframes(w.getnframes()))
peak = max(map(abs, s)) or 1
rms = math.sqrt(sum(x * x for x in s) / len(s)) or 1
print(f"peak {20 * math.log10(peak / 32768):6.1f} dBFS   rms {20 * math.log10(rms / 32768):6.1f} dBFS")
EOF
}

amixer -q -c$CARD sset Capture,0 80% cap

for input in "Rear Mic" "Front Mic" "Line"; do
    jack="$input Jack"
    [ "$input" = Line ] || amixer -q -c$CARD sset "$input Boost",0 67%
    amixer -q -c$CARD sset "Input Source",0 "$input"
    plugged=$(amixer -c$CARD cget iface=CARD,name="$jack" | sed -n 's/.*: values=//p')
    wav="/tmp/mic-test-$(echo "$input" | tr 'A-Z ' 'a-z-').wav"
    arecord -q -D hw:$CARD,0 -f S16_LE -r 48000 -c 2 -d "$SECS" "$wav" </dev/null
    printf '%-10s jack=%-3s  %s  %s\n' "$input" "$plugged" "$(level "$wav")" "$wav"
done
