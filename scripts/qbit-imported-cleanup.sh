#!/usr/bin/env bash
# Frees space in nas02:/mnt/SSD/torrents (refquota 200G, shared with cruncharr)
# by removing finished torrents that Sonarr/Radarr/Lidarr already imported.
#
# A torrent counts as imported when its hash appears as downloadId in an
# import event of any *arr history. Private-tracker torrents are listed but
# never deleted (they may have seeding obligations).
#
#   scripts/qbit-imported-cleanup.sh            # report only
#   scripts/qbit-imported-cleanup.sh --delete   # delete imported torrents + files
set -euo pipefail

CTX="${CTX:-lamg}"
NS="${NS:-piracy}"
DELETE=false; [ "${1:-}" = "--delete" ] && DELETE=true

K="kubectl --context=$CTX -n $NS"
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT

QBIT_PW=$($K get secret qbit-creds -o jsonpath='{.data.password}' | base64 -d)

qbit() { # qbit <curl args...> : runs curl against qBittorrent inside its pod
  $K exec -i deploy/qbittorrent-deployment -c qbittorrent -- sh -c "
    curl -s -c /tmp/qc -X POST 'http://localhost:8080/api/v2/auth/login' \
      -H 'Referer: http://localhost:8080' \
      --data-urlencode 'username=admin' --data-urlencode \"password=\$1\" >/dev/null
    shift; curl -s -b /tmp/qc \"\$@\"" _ "$QBIT_PW" "$@"
}

arr_imports() { # arr_imports <name> <port> <apiver> <eventType> : prints imported download hashes
  local name=$1 port=$2 ver=$3 ev=$4 key
  key=$($K exec "deploy/$name-deployment" -c "$name" -- \
    sh -c 'grep -o "<ApiKey>[^<]*" /config/config.xml | cut -c9-')
  $K exec "deploy/$name-deployment" -c "$name" -- \
    curl -sf -m 600 -H "X-Api-Key: $key" \
    "http://localhost:$port/api/$ver/history?page=1&pageSize=50000&eventType=$ev" |
    jq -r '.records[] | .downloadId // empty | ascii_upcase'
}

# eventType 3 = downloadFolderImported (sonarr/radarr), 8 = downloadImported (lidarr)
{ arr_imports sonarr 8989 v3 3; arr_imports radarr 7878 v3 3; arr_imports lidarr 8686 v1 8; } |
  sort -u > "$WORK/imported.txt"
qbit 'http://localhost:8080/api/v2/torrents/info' > "$WORK/qbit.json"

# state: progress==1 only; imported/private flags; size in GiB
jq -r --rawfile imp "$WORK/imported.txt" '
  ($imp | split("\n") | map(select(length>0)) | map({key:., value:true}) | from_entries) as $i
  | .[] | select(.progress == 1)
  | [ (if $i[(.hash|ascii_upcase)] then "IMPORTED" else "not-imported" end),
      (if .private then "private" else "public" end),
      (.size/1073741824*10|floor/10|tostring) + "G",
      .category, .hash, .name ] | @tsv' "$WORK/qbit.json" | sort > "$WORK/report.tsv"

column -t -s $'\t' "$WORK/report.tsv" | cut -c1-160
awk -F'\t' '{sub("G","",$3); s[$1" "$2]+=$3; n[$1" "$2]++}
  END {for (k in s) printf "%-24s %4d torrents %7.1fG\n", k, n[k], s[k]}' "$WORK/report.tsv"

$DELETE || exit 0
HASHES=$(awk -F'\t' '$1=="IMPORTED" && $2=="public" {print $5}' "$WORK/report.tsv" | paste -sd'|')
[ -n "$HASHES" ] || { echo "nothing to delete"; exit 0; }
qbit -X POST 'http://localhost:8080/api/v2/torrents/delete' \
  --data-urlencode "hashes=$HASHES" --data-urlencode 'deleteFiles=true'
echo "deleted $(tr '|' '\n' <<<"$HASHES" | wc -l) torrents"
