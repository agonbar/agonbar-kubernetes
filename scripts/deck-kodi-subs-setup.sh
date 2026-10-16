#!/usr/bin/env bash
# Install service.subs.castellano into the Deck's Kodi and stop the Jellyfin
# add-on from switching audio and subtitles ~2 s into playback (it picks the
# flagged-default dub, and re-opening subtitles drops every line already
# buffered). Idempotent; rerun after a Jellyfin add-on update. Kodi must not
# be running.
set -euo pipefail
cd "$(dirname "$0")"
deck=${DECK:-deck@192.168.1.218}
ssh_() { ssh -i ~/.ssh/nas -o BatchMode=yes "$deck" "$@"; }
D=.var/app/tv.kodi.Kodi/data

ssh_ 'pgrep -f "[k]odi.bin" >/dev/null' && { echo "Kodi is running; quit it first" >&2; exit 1; }

scp -q -r -i ~/.ssh/nas kodi/service.subs.castellano "$deck:$D/addons/"
scp -q -i ~/.ssh/nas kodi/keymaps/joystick.xml "$deck:$D/userdata/keymaps/joystick.xml"

ssh_ bash -s <<'EOF'
set -euo pipefail
D=~/.var/app/tv.kodi.Kodi/data
P=$D/addons/plugin.video.jellyfin/jellyfin_kodi/player.py
MARK="return  # local patch: audio and subtitles are chosen by service.subs.castellano"
# Drop the older patch, which only skipped the subtitle block.
sed -i "/return  # local patch: subtitles are chosen by service.subs.castellano/d" "$P"
if ! grep -qF "$MARK" "$P"; then
  # Make set_audio_subs a no-op right after it logs.
  sed -i "0,/^        LOG.info(\"Setting audio: %s subs: %s\", audio, subtitle)/s//&\n        $MARK/" "$P"
fi
grep -nF "$MARK" "$P"
DB=$D/userdata/Database/Addons33.db
sqlite3 "$DB" "INSERT OR IGNORE INTO update_rules(addonID, updateRule) VALUES('plugin.video.jellyfin', 1);"
sqlite3 "$DB" "SELECT addonID, updateRule FROM update_rules WHERE addonID='plugin.video.jellyfin';"
EOF
echo "Start Kodi, then: scripts/kodi-rpc.sh Addons.SetAddonEnabled '{\"addonid\":\"service.subs.castellano\",\"enabled\":true}'"
