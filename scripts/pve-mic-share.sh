#!/bin/bash
# Share proxmox-agb00's onboard microphone with the LAN. Run this ON the host.
# Idempotent.
#
#   ssh pve-agb00 bash -s < scripts/pve-mic-share.sh
#
# The onboard audio (0d:00.4, ALC1220) is not passed through to any VM, and
# passing it would give the mic to one VM only. So PipeWire runs here as the
# user micshare and pipewire-pulse serves the card on tcp:192.168.1.35:4713.
# Clients tunnel to it with scripts/mic-agb00-client.sh.
#
# pipewire-pulse has no authentication over TCP, which is why it listens on the
# LAN address and nothing else.
#
# The mic is an XLR condenser on the pink rear jack, wired straight to it with
# an XLR-to-jack cable and fed by the jack's own bias voltage. With the 48V
# phantom supply in between no audio got through (2026-10-13, cause not found).
#
# Test the jack without PipeWire in the way: scripts/pve-mic-test.sh

set -euo pipefail

ADDR=${ADDR:-192.168.1.35}
SOURCE=alsa_input.pci-0000_0d_00.4.analog-stereo
# 79% on PulseAudio's cubic scale is -6 dB: +20 dB boost and +30 dB capture.
# At 100% speech clips. "-6dB" would not do, pactl reads a signed value as a
# step relative to the current volume.
VOLUME=52057

DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
    pipewire pipewire-pulse wireplumber dbus-user-session pulseaudio-utils \
    alsa-utils alsa-ucm-conf >/dev/null

id micshare >/dev/null 2>&1 || useradd --system --create-home \
    --home-dir /var/lib/micshare --shell /usr/sbin/nologin --groups audio micshare
loginctl enable-linger micshare

uid=$(id -u micshare)
as_mic() {
    runuser -u micshare -- env XDG_RUNTIME_DIR=/run/user/$uid \
        DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$uid/bus "$@"
}

conf=/etc/pipewire/pipewire-pulse.conf.d/10-mic-share.conf
new=$(mktemp)
cat > "$new" <<EOF
# Share the onboard mic with the LAN: clients load module-tunnel-source against
# this address. Bound to the LAN IP only, pipewire-pulse has no auth over TCP.
pulse.properties = {
    server.address = [
        "unix:native"
        "tcp:$ADDR:4713"
    ]
}
EOF
changed=0
if ! cmp -s "$new" "$conf"; then
    install -D -m 644 "$new" "$conf"
    changed=1
fi
rm -f "$new"

for _ in $(seq 20); do [ -S /run/user/$uid/bus ] && break; sleep 1; done
as_mic systemctl --user enable --now pipewire.socket pipewire-pulse.socket \
    pipewire.service pipewire-pulse.service wireplumber.service
# Restarting drops every connected client, so only when the address changed.
[ $changed = 0 ] || as_mic systemctl --user restart pipewire-pulse

for _ in $(seq 20); do as_mic pactl get-source-volume $SOURCE >/dev/null 2>&1 && break; sleep 1; done
as_mic pactl set-source-volume $SOURCE $VOLUME

ss -ltn | grep -q "$ADDR:4713" || { echo "pipewire-pulse is not listening on $ADDR:4713" >&2; exit 1; }
echo "sharing $SOURCE on tcp:$ADDR:4713, $(as_mic pactl get-source-volume $SOURCE | head -1)"
