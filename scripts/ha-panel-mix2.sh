#!/usr/bin/env bash
# Push scripts/ha-panel-mix2.yaml to Home Assistant aya as the "panel-mix2"
# dashboard. Creates the dashboard the first time, then overwrites its config.
set -euo pipefail
cd "$(dirname "$0")"

ctx=lamg ns=aya url=panel-mix2
pod=$(kubectl --context $ctx -n $ns get pod -o name | grep -m1 homeassistant)
config=$(nix shell nixpkgs#yq-go -c yq -o=json -I=0 . ha-panel-mix2.yaml)

kubectl --context $ctx -n $ns exec -i "$pod" -- sh -c 'mkdir -p /tmp/s && cat > /tmp/s/ha-assist.py' < ha-assist.py
ws() { kubectl --context $ctx -n $ns exec "$pod" -- python3 /tmp/s/ha-assist.py --ws "$@"; }

if ! ws '{"type":"lovelace/dashboards/list"}' | grep -q "\"url_path\": \"$url\""; then
  ws "{\"type\":\"lovelace/dashboards/create\",\"url_path\":\"$url\",\"title\":\"Panel\",\"icon\":\"mdi:cellphone\",\"show_in_sidebar\":true,\"require_admin\":false,\"mode\":\"storage\"}" >/dev/null
  echo "created dashboard /$url"
fi
ws "{\"type\":\"lovelace/config/save\",\"url_path\":\"$url\",\"config\":$config}" | grep -q '"success": true'
echo "saved /$url"
