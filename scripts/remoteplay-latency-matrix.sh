#!/usr/bin/env bash
# Measure Steam Remote Play latency from the Bazzite VM (host) to the docked
# Steam Deck (client) for every codec x resolution x framerate, driving the
# Deck's Steam over CEF. Run from a workstation:
#
#   ssh -i ~/.ssh/nas -fN -L 18080:127.0.0.1:8080 deck@192.168.1.218
#   scripts/remoteplay-latency-matrix.sh [seconds-per-session] > matrix.tsv
#
# CODECS, RESOLUTIONS and FPS_LIST narrow the run. FOCUS_APPID (e.g. 427520,
# Factorio) is put in front on the host before each session, so Steam's
# screensaver doesn't end up being what gets streamed.
#
# Numbers come from the host's SessionStats block, written when each session
# ends. AvgFrameMS matches the overlay's "display latency"; AvgDisplayMS is
# skipped, a single outlier wrecks it. Stops the ambilight (gamescope-led-sync)
# while it runs: a second consumer on gamescope's PipeWire node crashes host
# Steam in NV12Scale during Pyrowave sessions. Restores the client config.
set -euo pipefail
cd "$(dirname "$0")/.."

SECS=${1:-25}
HOST_ID=15528753003057226758   # Bazzite VM's Remote Play clientId
K=(-i ~/.ssh/nas -o BatchMode=yes)
HOST=(ssh "${K[@]}" bazzite@192.168.1.212)
DECK=(ssh "${K[@]}" ${DECK:-deck@192.168.1.218})
cef() { scripts/steam-cef-eval.mjs "$1"; }
LOG='~/.local/share/Steam/logs/streaming_log.txt'

orig=$(cef "new Promise(r => SteamClient.RemotePlay.RegisterForSettingsChanges(s => r(btoa(String.fromCharCode(...new Uint8Array(s.RemotePlayClientConfig))))))" | tr -d '"')
cleanup() {
  cef "SteamClient.RemotePlay.SetStreamingClientConfig('$orig', 0)" >/dev/null || true
  "${HOST[@]}" 'systemctl --user start gamescope-led-sync' || true
}
trap cleanup EXIT
"${HOST[@]}" 'systemctl --user stop gamescope-led-sync'

# codec -> config fields: 13 hevc, 26 av1, 35 pyrowave (all off = H.264)
declare -A CODEC=([h264]="13=0 26=0 35=0" [hevc]="13=1 26=0 35=0" [av1]="13=0 26=1 35=0" [pyrowave]="13=0 26=0 35=1")
FIELDS="CaptureDescriptionID DecoderDescriptionID ResolutionX ResolutionY FramerateLimit AvgFPS AvgFrameMS AvgCaptureMS AvgConvertMS AvgEncodeMS AvgNetworkMS AvgDecodeMS AvgPingMS AvgServerBitrate"
echo -e "codec\treq_res\treq_fps\t${FIELDS// /\\t}\tcrashes\tfocused_app"

for codec in ${CODECS:-h264 hevc av1 pyrowave}; do
  for res in ${RESOLUTIONS:-1920x1080 2560x1440 3840x2160}; do
    for fps in ${FPS_LIST:-30 60}; do
      cfg=$(scripts/steam-remoteplay-config.mjs "$orig" ${CODEC[$codec]} 39=0 2=${res%x*} 3=${res#*x} 4=$((fps * 100)) 5=100)
      cef "SteamClient.RemotePlay.SetStreamingClientConfig('$cfg', 0)" >/dev/null
      [ -n "${FOCUS_APPID:-}" ] && "${HOST[@]}" "DISPLAY=:0 xprop -root -f GAMESCOPECTRL_BASELAYER_APPID 32co -set GAMESCOPECTRL_BASELAYER_APPID $FOCUS_APPID,769"
      before=$("${HOST[@]}" "grep -ac '\"SessionStats\"' $LOG || true")
      t0=$(date '+%H:%M:%S')
      cef "SteamClient.RemotePlay.StartDesktopStream('$HOST_ID')" >/dev/null
      for _ in $(seq 30); do
        "${DECK[@]}" 'pgrep -x streaming_clien >/dev/null && grep -aq StartVideoData /tmp/streaming_client.log' && break
        sleep 2
      done
      sleep "$SECS"
      focused=$("${HOST[@]}" "DISPLAY=:0 xprop -root GAMESCOPE_FOCUSED_APP | grep -oE '[0-9]+\$'")
      "${DECK[@]}" 'pkill -INT -f streaming_client' || true
      for _ in $(seq 30); do
        now=$("${HOST[@]}" "grep -ac '\"SessionStats\"' $LOG || true")
        [ "$now" -gt "$before" ] && break
        sleep 2
      done
      row=$("${HOST[@]}" "f=$LOG; n=\$(grep -an '\"SessionStats\"' \$f | tail -1 | cut -d: -f1); sed -n \"\${n},\$((n+60))p\" \$f" |
        awk -v fields="$FIELDS" 'BEGIN{n=split(fields,F," ")} {gsub(/"/,""); split($0,a,"\t+"); for(i=1;i<=n;i++) if($1==F[i]) {sub("^[ \t]*"F[i]"[ \t]+","",$0); v[F[i]]=$0}} END{for(i=1;i<=n;i++) printf "%s%s", v[F[i]], (i<n?"\t":"")}')
      crashes=$("${HOST[@]}" "coredumpctl list --since '$t0' --no-legend 2>/dev/null | wc -l")
      echo -e "$codec\t$res\t$fps\t$row\t$crashes\t$focused"
      for _ in $(seq 15); do "${DECK[@]}" 'pgrep -x streaming_clien >/dev/null' || break; sleep 2; done
      sleep 3
    done
  done
done
