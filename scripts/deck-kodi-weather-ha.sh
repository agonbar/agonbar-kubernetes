#!/usr/bin/env bash
# Point Kodi's weather.ha add-on on the Steam Deck at home-assistant-aya.
#
#   scripts/deck-kodi-weather-ha.sh            # KODI_HOST=deck@192.168.1.218 by default
#
# Mints a long-lived HA token ("Kodi Steam Deck") only if the add-on has none yet,
# then has Kodi itself store the settings through RunScript, so Kodi can keep
# running (it won't quit while Jellyfin syncs). The token never hits stdout.
set -euo pipefail
cd "$(dirname "$0")/.."
host=${KODI_HOST:-deck@192.168.1.218}
ha_url=${HA_URL:-https://home-aya.adriangonzalezbarbosa.eu}
data=/home/deck/.var/app/tv.kodi.Kodi/data
ssh_() { ssh -i ~/.ssh/nas -o BatchMode=yes "$host" "$@"; }

if ssh_ "grep -qE 'id=\"ha_key\">[^<]+<' $data/userdata/addon_data/weather.ha/settings.xml 2>/dev/null"; then
  echo "weather.ha already has a token, keeping it"
  token=
else
  token=$(kubectl --context=lamg -n aya exec -i deploy/homeassistant -- python3 - --ws \
      '{"type":"auth/long_lived_access_token","client_name":"Kodi Steam Deck","lifespan":3650}' \
      < scripts/ha-assist.py | python3 -c 'import json,sys; r=json.loads(sys.stdin.readline()); assert r["success"], r; print(r["result"])')
  echo "minted long-lived token 'Kodi Steam Deck'"
fi

# The token goes over ssh stdin into a 0600 file that the script deletes after reading.
printf %s "$token" | ssh_ "umask 077; cat > $data/weather-ha.token; cat > $data/weather-ha-set.py <<'EOF'
import os, xbmc, xbmcaddon
a = xbmcaddon.Addon('weather.ha')
p = '$data/weather-ha.token'
tok = open(p).read().strip()
os.remove(p)
if tok:
    a.setSetting('ha_key', tok)
a.setSetting('ha_server', '$ha_url')
a.setSetting('ha_weather_forecast_entity_id', 'weather.forecast_home')
a.setSetting('ha_sun_entity_id', 'sun.sun')
a.setSetting('loc_title', 'Ourense')
xbmc.executebuiltin('Weather.Refresh')
EOF
flatpak run --command=kodi-send tv.kodi.Kodi --action='RunScript($data/weather-ha-set.py)' >/dev/null"
sleep 8
ssh_ "rm -f $data/weather-ha-set.py; test ! -e $data/weather-ha.token || echo 'WARN: token file still there'"
scripts/kodi-rpc.sh XBMC.GetInfoLabels \
  '{"labels":["Weather.Location","Weather.Temperature","Weather.Conditions","Window(Weather).Property(Day0.HighTemperature)","Window(Weather).Property(Hourly.1.Temperature)"]}'
