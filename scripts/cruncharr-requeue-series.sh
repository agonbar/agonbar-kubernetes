#!/usr/bin/env bash
# Tira los releases de Cruncharr que una serie tiene en cola y reanuda la cola.
#
#   ./cruncharr-requeue-series.sh "Mushi-Shi"          # informe, no toca nada
#   ./cruncharr-requeue-series.sh "Mushi-Shi" --apply  # quita y reanuda la cola
#
# Un release de Cruncharr guarda el ID de serie de CR y la posición del episodio
# en su listado. Cuando Crunchyroll mueve una temporada a otra serie, lo que ya
# estaba en cola apunta a un listado donde el episodio no existe y falla con
# "Episode not found or not accessible". Cinco fallos seguidos paran la cola
# entera. En octubre de 2026 la temporada 2 de Mushi-Shi pasó de GRX0MGN76 a
# GT00099292 y 20 releases viejos dejaron 1092 descargas retenidas.
#
# Reintentar no sirve: el release viejo falla siempre. Hay que quitarlo y volver
# a pedirlo, que Cruncharr ya devuelve el ID nuevo. Sin blocklist: el hash es
# sha1(service:crEpisodeId) y bloquearlo bloquea ese ID para siempre.
#
# Si CR además le cambia el título a la temporada ("Mushi-Shi: The Next
# Passage"), Sonarr no sabe colocar el release nuevo y no entra. Eso se arregla
# en cruncharr, no aquí.
set -euo pipefail

CTX="${KUBE_CTX:-lamg}"
NS="${KUBE_NS:-piracy}"
TITLE="${1:?uso: $0 <título de la serie en Sonarr> [--apply]}"
APPLY=0; [ "${2:-}" = "--apply" ] && APPLY=1

K="kubectl --context=$CTX -n $NS"
trap '[ -n "${PF_PID:-}" ] && kill "$PF_PID" 2>/dev/null || true' EXIT

SONARR_KEY=$($K exec deploy/sonarr-deployment -c sonarr -- \
  sh -c 'grep -o "<ApiKey>[^<]*" /config/config.xml | cut -c9-')
$K port-forward svc/sonarr 18989:8989 >/dev/null 2>&1 & PF_PID=$!
sleep 3
SONARR="http://localhost:18989/api/v3"
api() { curl -sf -m 120 -H "X-Api-Key: $SONARR_KEY" -H "Content-Type: application/json" "$@"; }

SERIES_ID=$(api "$SONARR/series" | jq -r --arg t "$TITLE" \
  '.[] | select((.title|ascii_downcase) == ($t|ascii_downcase)) | .id')
[ -n "$SERIES_ID" ] || { echo "no hay serie \"$TITLE\" en Sonarr" >&2; exit 1; }

QUEUE=$(api "$SONARR/queue?seriesIds=$SERIES_ID&pageSize=1000&includeEpisode=true" |
  jq -c '[.records[] | select(.downloadClient == "Cruncharr")]')
N=$(jq length <<<"$QUEUE")
echo "serie $SERIES_ID \"$TITLE\": $N releases de Cruncharr en cola"
jq -r '.[] | "  S\(.episode.seasonNumber)E\(.episode.episodeNumber)\t\(.status)\t\(.title)"' <<<"$QUEUE" | sort -V
[ "$N" -gt 0 ] || exit 0
[ "$APPLY" = 1 ] || { echo "sin --apply no se toca nada"; exit 0; }

# Sin -f y con el código a la vista: un borrado a medias deja releases viejos
# en cola, y reanudar así vuelve a parar la cola a los cinco fallos.
CODE=$(curl -s -m 600 -o /dev/null -w '%{http_code}' -X DELETE \
  -H "X-Api-Key: $SONARR_KEY" -H "Content-Type: application/json" \
  -d "$(jq -c '{ids: map(.id)}' <<<"$QUEUE")" \
  "$SONARR/queue/bulk?removeFromClient=true&blocklist=false&skipRedownload=true") || true
LEFT=$(api "$SONARR/queue?seriesIds=$SERIES_ID&pageSize=1000" |
  jq '[.records[] | select(.downloadClient == "Cruncharr")] | length')
[ "$LEFT" = 0 ] || { echo "borrado incompleto (http $CODE), quedan $LEFT: vuelve a lanzarlo" >&2; exit 1; }
echo "quitados de Sonarr y de Cruncharr"

# La app escucha en la IP del pod, no en localhost.
$K exec deploy/cruncharr-deployment -c cruncharr -- sh -c \
  'curl -sf -m 60 -X POST -H "Authorization: Bearer $ANIDL_GUI_PASSWORD" http://$(hostname -i):3000/api/app/queue/resume' >/dev/null
echo "cola de Cruncharr reanudada"

# Una SeasonSearch de Sonarr no los recupera: en anime pregunta por alias y número
# absoluto ("Mushishi Zoku Shou 02") y Cruncharr contesta 0 a eso.
echo "para reencolar: scripts/sonarr-cruncharr-gap-fill.py --grab --all-series --series \"$TITLE\" --report \$(mktemp)"
