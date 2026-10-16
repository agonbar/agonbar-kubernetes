#!/usr/bin/env bash
# Collect evidence for the PyroWave colour bug while a Remote Play session from
# the Bazzite VM (host) to the Steam Deck (client) is running. Run from a
# workstation, once per test case:
#
#   scripts/pyrowave-evidence.sh <label> [outdir]
#
# Saves both screens (gamescopectl screenshot, no extra PipeWire consumer: a
# second consumer disconnecting crashes gamescope on NVIDIA), the host's capture
# method and PipeWire stream format, the negotiated format of gamescope's
# PipeWire node, and the client log. Only reads, changes nothing.
set -euo pipefail

label=$1
out=${2:-/tmp/pyrowave-evidence}/$label
mkdir -p "$out"
K=(-i ~/.ssh/nas -o BatchMode=yes)
HOST=${HOST:-bazzite@192.168.1.212}
DECK=${DECK:-deck@192.168.1.236} # .218 when docked on the wired hub

shot='rm -f /tmp/ev.png; gamescopectl screenshot /tmp/ev.png; for i in $(seq 10); do [ -s /tmp/ev.png ] && break; sleep 0.5; done'
ssh "${K[@]}" $HOST "$shot" & ssh "${K[@]}" $DECK "$shot" & wait
scp -q "${K[@]}" $HOST:/tmp/ev.png "$out/host.png"
scp -q "${K[@]}" $DECK:/tmp/ev.png "$out/deck.png"

ssh "${K[@]}" $HOST '
  f=~/.local/share/Steam/logs/streaming_log.txt
  # Last session only: from the last "Maximum capture" line on.
  awk "/Maximum capture/{buf=\"\"} {buf=buf \$0 \"\n\"} END{printf \"%s\", buf}" $f |
    grep -aE "Maximum capture|Capture method|Capture resolution|Capture colorspace|stream format changed|base bitrate|Pyrowave|DMA-BUF|dmabuf|Client video decoder"
  echo "--- gamescope PipeWire node, negotiated Format param"
  pw-dump 2>/dev/null | jq -c ".[] | select(.type==\"PipeWire:Interface:Node\" and (.info.props[\"node.name\"] // \"\" | test(\"gamescope\"))) | {id, name: .info.props[\"node.name\"], format: .info.params.Format}"
' > "$out/host.txt"

ssh "${K[@]}" $DECK '
  grep -avE "Marking frame|Vulkan device extension|SDL Mapping|HID device" /tmp/streaming_client.log | tail -60
  echo "--- frame results"; grep -aoE "status k_EStreamFrameResult[A-Za-z]+" /tmp/streaming_client.log | sort | uniq -c
' > "$out/deck.txt"

echo "$out:"; ls -la "$out"; grep -E "Capture method|stream format changed|modifier" "$out/host.txt" | tail -4
