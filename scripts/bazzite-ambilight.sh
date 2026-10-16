#!/usr/bin/env bash
# Game Mode ambilight on Bazzite: gamescope-led-sync reads gamescope's PipeWire
# stream and sends DRGB straight to the WLED behind the TV (no Hyperion).
# Run ON Bazzite with the binary next to this script or at $BIN:
#   scp gamescope-led-sync bazzite@<host>:/tmp/ && ssh bazzite@<host> BIN=/tmp/gamescope-led-sync bash -s < scripts/bazzite-ambilight.sh
#
# Only the patched build is safe (branch graceful-stop on top of upstream
# larry0x/gamescope-led-sync 1cf66aa): on SIGTERM it deactivates its stream so
# gamescope goes PAUSED before the disconnect. Upstream disconnects straight
# away, and gamescope 3.16.28-ogc3 can free the buffer it is painting into
# (SIGSEGV in paint_pipewire). For the same reason the unit never SIGKILLs it.
#
# The unit idles in Desktop Mode (no gamescope node) and reconnects when Game
# Mode comes back. Layout: 226 LEDs from the bottom-right corner, clockwise.
set -euo pipefail

BIN=${BIN:-./gamescope-led-sync}
WLED=${WLED:-192.168.1.20}
UNIT=$HOME/.config/systemd/user/gamescope-led-sync.service

install -Dm755 "$BIN" "$HOME/.local/bin/gamescope-led-sync"
mkdir -p "$(dirname "$UNIT")"
cat > "$UNIT" <<EOF
[Unit]
Description=Ambilight: gamescope screen edges to the WLED at $WLED
After=pipewire.service

[Service]
ExecStart=%h/.local/bin/gamescope-led-sync --wled $WLED --start br --top 73 --right 40 --bottom 73 --left 40
KillSignal=SIGTERM
SendSIGKILL=no
TimeoutStopSec=10
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable gamescope-led-sync.service
systemctl --user restart gamescope-led-sync.service
systemctl --user --no-pager status gamescope-led-sync.service | head -5
