#!/usr/bin/env bash
# Triage finished Sonarr torrents that never got imported. Many were grabbed
# for Castellano subs the library file lacks, and Sonarr cannot see that: it
# scores Castellano by a language custom format, not by audio vs subtitles,
# so it rejects them as "not an upgrade" and leaves them seeding forever.
#
# Source is qBittorrent (category tv-sonarr, 100 %), not Sonarr's queue: once
# qBittorrent restarts, Sonarr stops tracking stopped torrents. Each torrent
# goes through Sonarr's manual-import parser, then its Castellano tracks
# (spa, minus Latino / LatAm / 419) are compared with the library files:
#   FILL       some of its episodes have no file: import it
#   ADDS-CAST  it has Castellano audio or subs the library lacks: import it
#   REDUNDANT  every episode has a file and it adds no Castellano: delete it
#   UNMAPPED   Sonarr cannot map it to episodes: left alone
# Imports use copy mode; delete the imported torrents afterwards with
# scripts/qbit-imported-cleanup.sh --delete.
#
#   scripts/sonarr-nonupgrade-triage.sh           # report
#   scripts/sonarr-nonupgrade-triage.sh --apply   # import FILL/ADDS-CAST, delete REDUNDANT
set -euo pipefail

CTX="${CTX:-lamg}"; NS="${NS:-piracy}"
APPLY=false; [ "${1:-}" = "--apply" ] && APPLY=true
K="kubectl --context=$CTX -n $NS"
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT

# kubectl exec drops with EOF/502 when a node is busy; retry every pod call
retry() { local in out i; in=$(cat)
  for i in 1 2 3 4 5 6; do out=$("$@" <<<"$in") && { printf '%s\n' "$out"; return 0; }; sleep 20; done
  return 1; }

QBIT_PW=$($K get secret qbit-creds -o jsonpath='{.data.password}' | base64 -d)
qbit() { retry qbit_once "$@"; }
qbit_once() {
  $K exec -i deploy/qbittorrent-deployment -c qbittorrent -- sh -c "
    curl -s -c /tmp/qc -X POST 'http://localhost:8080/api/v2/auth/login' \
      -H 'Referer: http://localhost:8080' \
      --data-urlencode 'username=admin' --data-urlencode \"password=\$1\" >/dev/null
    shift; curl -s -b /tmp/qc \"\$@\"" _ "$QBIT_PW" "$@"
}
KEY=$($K exec deploy/sonarr-deployment -c sonarr -- \
  sh -c 'grep -o "<ApiKey>[^<]*" /config/config.xml | cut -c9-')
sonarr() { retry sonarr_once "$@"; } # sonarr <method> <path> [json body on stdin]
sonarr_once() {
  $K exec -i deploy/sonarr-deployment -c sonarr -- curl -sf -m 600 -X "$1" \
    -H "X-Api-Key: $KEY" -H 'Content-Type: application/json' \
    --data-binary @- "http://localhost:8989/api/v3/$2"
}

# castellano <file...> : prints "<audio> <subs>" counts of Castellano tracks
castellano() { retry castellano_once "$@" </dev/null; }
castellano_once() {
  $K exec -i deploy/sonarr-deployment -c sonarr -- sh -c '
    a=0; s=0
    for f in "$@"; do
      [ -f "$f" ] || continue
      out=$(/app/sonarr/bin/ffprobe -v error -show_entries stream=codec_type:stream_tags=language,title \
              -of csv=p=0 "$f" 2>/dev/null | grep -iE ",(spa|es|esp)(,|$)|castellano|espa" |
            grep -viE "latin|latam|419|mexic")
      a=$((a + $(printf "%s\n" "$out" | grep -c "^audio")))
      s=$((s + $(printf "%s\n" "$out" | grep -c "^subtitle")))
    done
    echo "$a $s"' _ "$@"
}

qbit http://localhost:8080/api/v2/torrents/info </dev/null |
  jq -c '.[] | select(.category == "tv-sonarr" and .progress == 1)
         | {hash: (.hash | ascii_upcase), name, path: .content_path}' > "$WORK/torrents.jsonl"

: > "$WORK/import.jsonl"; : > "$WORK/delete.txt"
while IFS= read -r t; do
  name=$(jq -r .name <<<"$t"); path=$(jq -r .path <<<"$t"); hash=$(jq -r .hash <<<"$t")
  sonarr GET "manualimport?filterExistingFiles=false&folder=$(jq -rn --arg f "$path" '$f|@uri')" </dev/null \
    > "$WORK/mi.json" 2>/dev/null || echo '[]' > "$WORK/mi.json"
  jq -c '[.[] | select((.episodes | length) > 0)]' "$WORK/mi.json" > "$WORK/items.json"
  if [ "$(jq length "$WORK/items.json")" -eq 0 ]; then
    printf '%-9s %s\n' UNMAPPED "${name:0:90}"; continue
  fi
  verdict=REDUNDANT; detail=""
  while IFS= read -r it; do
    lib=()
    for ep in $(jq -r '.episodes[].id' <<<"$it"); do
      p=$(sonarr GET "episode/$ep" </dev/null | jq -r '.episodeFile.path // empty')
      if [ -n "$p" ]; then lib+=("$p"); else verdict=FILL; fi
    done
    read -r na ns < <(castellano "$(jq -r .path <<<"$it")")
    read -r oa os < <(castellano "${lib[@]}")
    detail="new a$na/s$ns lib a$oa/s$os"
    if [ "$verdict" = REDUNDANT ] && { { [ "$na" -gt 0 ] && [ "$oa" -eq 0 ]; } || { [ "$ns" -gt 0 ] && [ "$os" -eq 0 ]; }; }; then
      verdict=ADDS-CAST
    fi
  done < <(jq -c '.[]' "$WORK/items.json")
  printf '%-9s %-22s %s\n' "$verdict" "$detail" "${name:0:80}"
  case $verdict in
    FILL|ADDS-CAST) jq -c --arg h "$hash" '.[] | {path, folderName, seriesId: .series.id,
        episodeIds: [.episodes[].id], quality, languages, releaseGroup, indexerFlags,
        releaseType, downloadId: $h}' "$WORK/items.json" >> "$WORK/import.jsonl" ;;
    REDUNDANT) echo "$hash" >> "$WORK/delete.txt" ;;
  esac
done < "$WORK/torrents.jsonl"

echo "to import: $(wc -l < "$WORK/import.jsonl") files; to delete: $(wc -l < "$WORK/delete.txt") torrents"
$APPLY || exit 0

if [ -s "$WORK/import.jsonl" ]; then
  jq -sc '{name: "ManualImport", importMode: "copy", files: .}' "$WORK/import.jsonl" |
    sonarr POST command | jq -r '"ManualImport command \(.id) \(.status)"'
fi
if [ -s "$WORK/delete.txt" ]; then
  qbit -X POST http://localhost:8080/api/v2/torrents/delete \
    --data-urlencode "hashes=$(tr '\n' '|' < "$WORK/delete.txt" | sed 's/|$//' | tr 'A-Z' 'a-z')" \
    --data deleteFiles=true </dev/null
  echo "deleted $(wc -l < "$WORK/delete.txt") torrents"
fi
