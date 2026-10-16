#!/bin/bash
# Add proxmox-agb00's shared microphone as an input on this machine. Run this ON
# the client: Bazzite, the Steam Deck, any PipeWire desktop on the LAN.
# Idempotent.
#
#   ./mic-agb00-client.sh
#
# The mic is an XLR condenser on the pink rear jack of agb00, wired straight to
# the jack with no phantom supply. The host owns the onboard audio, so it runs
# PipeWire as user micshare and serves the mic on tcp:192.168.1.35:4713
# (/etc/pipewire/pipewire-pulse.conf.d/10-mic-share.conf over there). This
# writes a PipeWire drop-in that tunnels to it. The mic shows up as "Micro
# agb00" and reconnects by itself after agb00 or the network was away.
#
# NixOS hosts load the same module from their flake instead of running this.
#
# Restarting PipeWire cuts this machine's audio for a second.

set -euo pipefail

SERVER=${SERVER:-tcp:192.168.1.35:4713}
conf=~/.config/pipewire/pipewire.conf.d/20-mic-agb00.conf

mkdir -p "$(dirname "$conf")"
cat > "$conf" <<EOF
context.modules = [
    { name = libpipewire-module-pulse-tunnel
      args = {
          tunnel.mode = source
          pulse.server.address = "$SERVER"
          reconnect.interval.ms = 5000
          audio.channels = 1
          audio.position = [ MONO ]
          stream.props = {
              node.name = "mic-agb00"
              node.description = "Micro agb00"
          }
      }
      flags = [ nofail ]
    }
]
EOF

systemctl --user restart pipewire pipewire-pulse
sleep 3
wpctl status | grep -F "Micro agb00" || {
    echo "Micro agb00 did not show up, is $SERVER reachable from here?" >&2
    exit 1
}
