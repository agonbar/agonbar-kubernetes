#!/usr/bin/env bash
# Reproduce "subs missing at episode start" on the Deck's Kodi and capture
# screenshots at known cue times, muted.
#
#   scripts/kodi-subs-probe.sh <episodeid> <sub-name-regex> <mode> <t1> [t2 ...]
#
# mode: none     play and let Kodi/the Jellyfin add-on pick the subtitle
#       switch   at 4 s switch to the subtitle whose name matches the regex
#       seek     like switch, then seek back to 0
# Screenshots land in /tmp/kodishot/<mode>-<t>.png
set -euo pipefail
cd "$(dirname "$0")/.."
ep=$1 want=$2 mode=$3; shift 3
R=scripts/kodi-rpc.sh
deck="ssh -i $HOME/.ssh/nas -o BatchMode=yes deck@192.168.1.218"
mkdir -p /tmp/kodishot

now() { $R Player.GetProperties '{"playerid":1,"properties":["time"]}' |
  jq '.time.hours*3600+.time.minutes*60+.time.seconds+.time.milliseconds/1000'; }
at() { until jq -e -n "$(now) >= $1" >/dev/null; do :; done; }
shot() {
  $deck "F=/home/deck/.var/app/tv.kodi.Kodi/data/shot.png; rm -f \$F; flatpak run --command=kodi-send tv.kodi.Kodi --action=\"TakeScreenshot(\$F,sync)\" >/dev/null; sleep 1"
  scp -q -i ~/.ssh/nas deck@192.168.1.218:.var/app/tv.kodi.Kodi/data/shot.png "/tmp/kodishot/$1.png"
}

$R Application.SetMute '{"mute":true}' >/dev/null
$R Player.Open "{\"item\":{\"episodeid\":$ep},\"options\":{\"resume\":false}}" >/dev/null
until [[ $($R Player.GetActivePlayers) == *'"playerid": 1'* ]]; do :; done
at 0.5

if [[ $mode != none ]]; then
  at 4
  idx=$($R Player.GetProperties '{"playerid":1,"properties":["subtitles"]}' |
    jq --arg re "$want" '[.subtitles[] | select(.name|test($re;"i"))][0].index')
  echo "switching to subtitle index $idx"
  $R Player.SetSubtitle "{\"playerid\":1,\"subtitle\":$idx,\"enable\":true}" >/dev/null
  [[ $mode == seek ]] && $R Player.Seek '{"playerid":1,"value":{"time":{"hours":0,"minutes":0,"seconds":0,"milliseconds":0}}}' >/dev/null
fi

# Pause for each shot: a screenshot takes ~2 s and would miss short cues.
for t in "$@"; do
  at "$t"; $R Player.PlayPause '{"playerid":1,"play":false}' >/dev/null
  echo "shot $mode-$t at $(now)"; shot "$mode-$t"
  $R Player.PlayPause '{"playerid":1,"play":true}' >/dev/null
done
$R Player.GetProperties '{"playerid":1,"properties":["currentsubtitle","subtitleenabled"]}' | jq -c '{sub:.currentsubtitle.name, on:.subtitleenabled}'
$R Player.Stop '{"playerid":1}' >/dev/null
$R Application.SetMute '{"mute":false}' >/dev/null
