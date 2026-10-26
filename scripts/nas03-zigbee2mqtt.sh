#!/usr/bin/env bash
# zigbee2mqtt for aya, as the TrueNAS community app on nas03, where the CC2531 stick is.
#
# The broker is NOT here: it is deployments/aya/mqtt.yaml in k3s, reached through the
# kube-vip-aya VIP. The app demands a user and password, but that EMQX has no
# authenticator and accepts any pair, same as lamg's, so these are placeholders.
#
#   ./nas03-zigbee2mqtt.sh          # create the app, or update it if it already exists
#   ./nas03-zigbee2mqtt.sh status
#
# The app only accepts /dev/serial/by-id/ paths; it adds the device and the dialout
# group (20) on its own. Web UI: http://192.168.1.34:30065
set -euo pipefail

HOST=${HOST:-nas03-lan}
APP=zigbee2mqtt
BROKER=${BROKER:-mqtt://192.168.1.26:1883}
PORT=${PORT:-30065}

n03(){ ssh -o BatchMode=yes "$HOST" "$@"; }
log(){ echo "[$(date +%H:%M:%S)] $*"; }

if [ "${1:-}" = status ]; then
  n03 "midclt call app.query '[[\"name\",\"=\",\"$APP\"]]'" | python3 -c '
import json,sys
a=json.load(sys.stdin)
print(a[0]["name"], a[0]["state"], a[0]["version"]) if a else print("no app")'
  exit 0
fi

dev=$(n03 'ls /dev/serial/by-id/' | grep -i cc2531 | head -1)
[ -n "$dev" ] || { log "no CC2531 under /dev/serial/by-id on $HOST"; exit 1; }
dev=/dev/serial/by-id/$dev
log "stick: $dev"

values=$(python3 - "$dev" "$BROKER" "$PORT" <<'EOF'
import json, sys
dev, broker, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
print(json.dumps({
  "TZ": "Europe/Madrid",
  "zigbee2mqtt": {
    "mqtt": {"topic": "zigbee2mqtt", "server": broker, "user": "zigbee2mqtt", "password": "anonymous"},
    "serial": {"port": dev, "adapter": "zstack"},
    # z2m 2.x ships with HA discovery off; without this HA sees nothing.
    "additional_envs": [{"name": "ZIGBEE2MQTT_CONFIG_HOMEASSISTANT_ENABLED", "value": "true"}],
  },
  "network": {"web_port": {"bind_mode": "published", "port_number": port}},
}))
EOF
)

if n03 "midclt call app.query '[[\"name\",\"=\",\"$APP\"]]'" | grep -q '"name"'; then
  log "app exists, updating values"
  printf '%s' "{\"values\": $values}" | n03 "cat > /tmp/z2m.json && midclt call -j app.update $APP \"\$(cat /tmp/z2m.json)\" >/dev/null; rc=\$?; rm -f /tmp/z2m.json; exit \$rc"
else
  log "creating app"
  printf '%s' "{\"app_name\": \"$APP\", \"catalog_app\": \"zigbee2mqtt\", \"train\": \"community\", \"values\": $values}" \
    | n03 "cat > /tmp/z2m.json && midclt call -j app.create \"\$(cat /tmp/z2m.json)\" >/dev/null; rc=\$?; rm -f /tmp/z2m.json; exit \$rc"
fi
"$0" status
