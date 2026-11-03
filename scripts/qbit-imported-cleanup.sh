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

# Sonarr's /history 500s on a series with a malformed Actors column and is
# ~1.5 s/record with includeSeries=false (16k+ records), so read its Postgres.
sonarr_imports() {
  local cfg
  cfg=$($K exec deploy/sonarr-deployment -c sonarr -- cat /config/config.xml)
  xml() { grep -o "<$1>[^<]*" <<<"$cfg" | cut -d'>' -f2; }
  # podAffinity: only sonarr's node has a route to the nas02 LAN address
  xml PostgresPassword | $K run "psql-$$" --rm -i -q --restart=Never \
    --image=postgres:17-alpine --overrides='{"spec":{"affinity":{"podAffinity":{
      "requiredDuringSchedulingIgnoredDuringExecution":[{"topologyKey":"kubernetes.io/hostname",
      "labelSelector":{"matchLabels":{"app":"sonarr"}}}]}}}}' --command -- sh -c "read -r PGPASSWORD; export PGPASSWORD
      psql -h $(xml PostgresHost) -p $(xml PostgresPort) -U $(xml PostgresUser) -d $(xml PostgresMainDb) -At \
        -c 'SELECT DISTINCT upper(\"DownloadId\") FROM \"History\" WHERE \"EventType\" = 3 AND \"DownloadId\" IS NOT NULL'"
}

arr_queue() { # arr_queue <name> <port> <apiver> : prints hashes still in the *arr queue
  local name=$1 port=$2 ver=$3 key
  key=$($K exec "deploy/$name-deployment" -c "$name" -- \
    sh -c 'grep -o "<ApiKey>[^<]*" /config/config.xml | cut -c9-')
  $K exec "deploy/$name-deployment" -c "$name" -- \
    curl -sf -m 300 -H "X-Api-Key: $key" "http://localhost:$port/api/$ver/queue?page=1&pageSize=5000" |
    jq -r '.records[] | .downloadId // empty | ascii_upcase'
}

# eventType 3 = downloadFolderImported (radarr), 8 = downloadImported (lidarr)
{ sonarr_imports; arr_imports radarr 7878 v3 3; arr_imports lidarr 8686 v1 8; } |
  sort -u > "$WORK/imported.txt"
# A torrent can be imported and still have pending imports (a season pack
# imported halfway): anything still queued is never deleted.
{ arr_queue sonarr 8989 v3; arr_queue radarr 7878 v3; arr_queue lidarr 8686 v1; } |
  sort -u > "$WORK/queued.txt"
qbit 'http://localhost:8080/api/v2/torrents/info' > "$WORK/qbit.json"

# state: progress==1 only; imported/private flags; size in GiB
jq -r --rawfile imp "$WORK/imported.txt" --rawfile que "$WORK/queued.txt" '
  def set: split("\n") | map(select(length>0)) | map({key:., value:true}) | from_entries;
  ($imp | set) as $i | ($que | set) as $q
  | .[] | select(.progress == 1)
  | [ (if $q[(.hash|ascii_upcase)] then "in-queue"
       elif $i[(.hash|ascii_upcase)] then "IMPORTED" else "not-imported" end),
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
