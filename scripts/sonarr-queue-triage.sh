#!/usr/bin/env bash
# Clasifica la cola de Sonarr cruzándola con el estado real de qBittorrent y,
# con --apply, borra las descargas muertas (blocklist + rebúsqueda).
#
#   ./sonarr-queue-triage.sh            # informe, no toca nada
#   ./sonarr-queue-triage.sh --apply    # además borra las de la clase dead-swarm/unreachable
#
# Complementa a qbit-stale-cleanup (CronJob semanal, criterio downloaded==0 + 7d
# de inactividad): esto mira la cola desde el lado de Sonarr y agrupa las filas
# por descarga, que es donde los packs de temporada inflan el recuento.
set -euo pipefail

CTX="${KUBE_CTX:-lamg}"
NS="${KUBE_NS:-piracy}"
APPLY=0; [ "${1:-}" = "--apply" ] && APPLY=1

# Umbrales
DEAD_AGE_DAYS="${DEAD_AGE_DAYS:-2}"        # sin ningún seeder en tracker y más viejo que esto
UNREACH_AGE_DAYS="${UNREACH_AGE_DAYS:-7}"  # hay seeders en tracker pero no conectamos
UNREACH_PROGRESS="${UNREACH_PROGRESS:-50}" # ...y va por debajo de este %

K="kubectl --context=$CTX -n $NS"
WORK=$(mktemp -d); trap 'rm -rf "$WORK"; [ -n "${PF_PID:-}" ] && kill "$PF_PID" 2>/dev/null || true' EXIT

# --- credenciales, leídas del cluster, nunca hardcodeadas ---
SONARR_KEY=$($K exec deploy/sonarr-deployment -c sonarr -- \
  sh -c 'grep -o "<ApiKey>[^<]*" /config/config.xml | cut -c9-')
QBIT_PW=$($K get secret qbit-creds -o jsonpath='{.data.password}' | base64 -d)

$K port-forward svc/sonarr 18989:8989 >/dev/null 2>&1 & PF_PID=$!
sleep 3
SONARR="http://localhost:18989/api/v3"
curl -sf -m 60 -H "X-Api-Key: $SONARR_KEY" \
  "$SONARR/queue?page=1&pageSize=1000&includeUnknownSeriesItems=true&includeSeries=true" -o "$WORK/queue.json"

$K exec deploy/qbittorrent-deployment -c qbittorrent -- sh -c "
  curl -s -c /tmp/qc -X POST 'http://localhost:8080/api/v2/auth/login' \
    -H 'Referer: http://localhost:8080' \
    --data-urlencode 'username=admin' --data-urlencode 'password=$QBIT_PW' >/dev/null
  curl -s -b /tmp/qc 'http://localhost:8080/api/v2/torrents/info'" > "$WORK/qbit.json"

# --- clasificación: una fila por descarga, no por episodio ---
jq -r --slurpfile qb "$WORK/qbit.json" \
   --argjson dead_age "$DEAD_AGE_DAYS" \
   --argjson unreach_age "$UNREACH_AGE_DAYS" \
   --argjson unreach_prog "$UNREACH_PROGRESS" '
  ($qb[0] | map({key: (.hash|ascii_upcase), value: .}) | from_entries) as $t
  | [.records[]]
  | group_by(.downloadId)
  | map(. as $rows | $rows[0] as $r | (if $r.downloadId then ($t[$r.downloadId] // null) else null end) as $q | {
      rows: ($rows|length),
      id: $r.downloadId,
      client: ($r.downloadClient // "?"),
      title: $r.title,
      class:
        if $r.downloadClient != "qBittorrent" then "otro-cliente"
        elif $r.downloadId == null then "sin-descarga"
        elif $q == null then "huerfano"
        elif $r.trackedDownloadState == "importBlocked" then "import-bloqueado"
        elif $r.trackedDownloadState == "importPending" then "import-pendiente"
        elif $q.num_complete == 0 and ((now - $q.added_on)/86400) >= $dead_age then "swarm-muerto"
        elif $q.num_seeds == 0 and ($q.progress*100) < $unreach_prog
             and ((now - $q.added_on)/86400) >= $unreach_age then "inalcanzable"
        else "sano" end,
      age: (if $q then ((now - $q.added_on)/86400|floor) else -1 end),
      idle: (if $q then ((now - $q.last_activity)/86400|floor) else -1 end),
      prog: (if $q then ($q.progress*100|floor) else -1 end),
      seeds: (if $q then "\($q.num_seeds)/\($q.num_complete)" else "-" end)
    })
  | sort_by(.class, -.rows)
  | (["FILAS","CLASE","EDAD","IDLE","PROG","SEEDS","CLIENTE","TITULO"] | @tsv),
    (.[] | [.rows, .class, "\(.age)d", "\(.idle)d", "\(.prog)%", .seeds, .client, (.title[0:58])] | @tsv),
    "",
    ("RESUMEN: \(length) descargas / \([.[].rows]|add) filas de cola"),
    (group_by(.class) | map("  \(.[0].class): \(length) descargas, \([.[].rows]|add) filas") | .[])
' "$WORK/queue.json" | column -t -s $'\t'

# --- acción ---
REMOVE_IDS=$(jq -r --slurpfile qb "$WORK/qbit.json" \
   --argjson dead_age "$DEAD_AGE_DAYS" --argjson unreach_age "$UNREACH_AGE_DAYS" \
   --argjson unreach_prog "$UNREACH_PROGRESS" '
  ($qb[0] | map({key: (.hash|ascii_upcase), value: .}) | from_entries) as $t
  | [.records[] | select(.downloadClient == "qBittorrent")
     | . as $r | (if $r.downloadId then ($t[$r.downloadId] // null) else null end) as $q
     | select($q != null)
     | select($r.trackedDownloadState == "downloading")
     | select(($q.num_complete == 0 and ((now - $q.added_on)/86400) >= $dead_age)
              or ($q.num_seeds == 0 and ($q.progress*100) < $unreach_prog
                  and ((now - $q.added_on)/86400) >= $unreach_age))
     | .id]
  | join(",")' "$WORK/queue.json")

if [ -z "$REMOVE_IDS" ]; then
  echo; echo "Nada que borrar."
  exit 0
fi

COUNT=$(echo "$REMOVE_IDS" | tr ',' '\n' | wc -l)
if [ "$APPLY" -eq 0 ]; then
  echo; echo "--apply borraría $COUNT filas de cola (blocklist + rebúsqueda automática)."
  exit 0
fi

echo; echo "Borrando $COUNT filas de cola..."
curl -sf -X DELETE -H "X-Api-Key: $SONARR_KEY" \
  "$SONARR/queue/bulk?removeFromClient=true&blocklist=true&skipRedownload=false" \
  -H 'Content-Type: application/json' \
  -d "{\"ids\":[${REMOVE_IDS}]}" >/dev/null
echo "Hecho."
