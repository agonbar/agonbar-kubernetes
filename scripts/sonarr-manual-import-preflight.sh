#!/usr/bin/env bash
# Los 4 checks de runbooks/sonarr-manual-import-safety-checks.md, ejecutables.
#
#   ./sonarr-manual-import-preflight.sh 1737189451          # informe, no toca nada
#   ./sonarr-manual-import-preflight.sh 1737189451 --apply  # además fuerza el import
#
# Forzar un ManualImport con episodeIds explícitos se salta el perfil de calidad
# Y la blocklist. En agosto de 2026 eso importó un pack raw japonés ya
# blocklisteado encima de tres ficheros buenos con subs, sin recycleBin para
# recuperarlos. El runbook lo dejó escrito como prosa; esto lo comprueba.
#
# Un check en rojo aborta. El único que avisa pero no aborta es el de
# recycleBin: sin él, el fichero que se sustituye se pierde para siempre.
set -euo pipefail

CTX="${KUBE_CTX:-lamg}"
NS="${KUBE_NS:-piracy}"
QUEUE_ID="${1:?uso: $0 <sonarr-queue-id> [--apply]}"
APPLY=0; [ "${2:-}" = "--apply" ] && APPLY=1

K="kubectl --context=$CTX -n $NS"
WORK=$(mktemp -d); trap 'rm -rf "$WORK"; [ -n "${PF_PID:-}" ] && kill "$PF_PID" 2>/dev/null || true' EXIT

SONARR_KEY=$($K exec deploy/sonarr-deployment -c sonarr -- \
  sh -c 'grep -o "<ApiKey>[^<]*" /config/config.xml | cut -c9-')
$K port-forward svc/sonarr 18989:8989 >/dev/null 2>&1 & PF_PID=$!
sleep 3
SONARR="http://localhost:18989/api/v3"
api() { curl -sf -m 120 -H "X-Api-Key: $SONARR_KEY" "$@"; }

# ffprobe no está en sonarr. cruncharr monta /downloads y jellyfin monta la
# biblioteca en /media, así que cada ruta se sondea desde el pod que la ve.
probe() {
  local path="$1"
  case "$path" in
    /downloads/*) $K exec deploy/cruncharr-deployment -c cruncharr -- \
        ffprobe -v error -show_entries stream=codec_type:stream_tags=language \
        -of json "$path" ;;
    /tv/*)        $K exec deploy/jellyfin-deployment -c jellyfin -- \
        /usr/lib/jellyfin-ffmpeg/ffprobe -v error -show_entries stream=codec_type:stream_tags=language \
        -of json "/media/tv/${path#/tv/}" ;;
    *) echo '{"streams":[]}' ;;
  esac
}
subs_of() { jq -r '[.streams[]|select(.codec_type=="subtitle")|.tags.language//"und"]|join(",")'; }

api "$SONARR/queue?page=1&pageSize=1000" \
  | jq --argjson id "$QUEUE_ID" '.records[]|select(.id==$id)' > "$WORK/q.json"
[ -s "$WORK/q.json" ] || { echo "queue id $QUEUE_ID no está en la cola"; exit 1; }

DL_ID=$(jq -r .downloadId "$WORK/q.json")
echo "== $(jq -r .title "$WORK/q.json")"
echo "   estado: $(jq -r '.status+" / "+.trackedDownloadState' "$WORK/q.json")"

api "$SONARR/manualimport?downloadId=$DL_ID&filterExistingFiles=false" \
  | jq '.[0]' > "$WORK/mi.json"
SERIES_ID=$(jq -r '.series.id' "$WORK/mi.json")
NEW_PATH=$(jq -r '.path' "$WORK/mi.json")
NEW_SCORE=$(jq -r '.customFormatScore' "$WORK/mi.json")
EPISODE_IDS=$(jq -c '[.episodes[].id]' "$WORK/mi.json")
[ "$SERIES_ID" = "null" ] && { echo "sonarr no mapea el fichero a ninguna serie"; exit 1; }
echo "   mapeo:  serie=$SERIES_ID episodios=$EPISODE_IDS"
echo "   rechazo: $(jq -r '[.rejections[].reason]|join("; ")' "$WORK/mi.json")"

FAIL=0
red()  { echo "  FALLA  $*"; FAIL=1; }
ok()   { echo "  ok     $*"; }
warn() { echo "  AVISO  $*"; }

echo
echo "1) blocklist de la serie"
BL=$(api "$SONARR/blocklist?page=1&pageSize=500&sortKey=date&sortDirection=descending" \
     | jq --argjson s "$SERIES_ID" '[.records[]|select(.seriesId==$s)]')
BL_N=$(echo "$BL" | jq length)
if [ "$BL_N" -gt 0 ]; then
  # Un pack blocklisteado deja una fila por episodio; agrupa por sourceTitle.
  echo "$BL" | jq -r '.[].sourceTitle' | sort -u | head -5 | sed 's/^/         /'
  red "$BL_N entradas en la blocklist para esta serie — comprueba que no es esta"
else ok "sin entradas"; fi

echo "2) custom format score contra el perfil"
PROFILE_ID=$(api "$SONARR/series/$SERIES_ID" | jq -r .qualityProfileId)
MIN_SCORE=$(api "$SONARR/qualityprofile/$PROFILE_ID" | jq -r '.minFormatScore')
if [ "$NEW_SCORE" = "null" ]; then
  red "sonarr no devuelve customFormatScore para este fichero"
elif [ "$NEW_SCORE" -lt "$MIN_SCORE" ]; then
  red "score $NEW_SCORE < minFormatScore $MIN_SCORE — sonarr nunca lo aceptaría solo"
else ok "score $NEW_SCORE >= minFormatScore $MIN_SCORE"; fi

echo "3) pistas reales del fichero (ffprobe, no mediaInfo)"
NEW_SUBS=$(probe "$NEW_PATH" | subs_of)
if [ -z "$NEW_SUBS" ]; then red "el fichero entrante no tiene NINGUNA pista de subtítulos"
else ok "entra: subs=[$NEW_SUBS]"; fi

echo "4) recycleBin"
RB=$(api "$SONARR/config/mediamanagement" | jq -r '.recycleBin')
if [ -z "$RB" ] || [ "$RB" = "null" ]; then
  warn "sin configurar — lo que se sustituya se borra sin vuelta atrás"
else ok "configurado en $RB"; fi

echo
echo "se sustituyen:"
for EP in $(echo "$EPISODE_IDS" | jq -r '.[]'); do
  FID=$(api "$SONARR/episode/$EP" | jq -r '.episodeFileId // empty')
  if [ -z "$FID" ] || [ "$FID" = "0" ]; then echo "   ep $EP: sin fichero previo"; continue; fi
  api "$SONARR/episodefile/$FID" > "$WORK/f.json"
  OLD_PATH=$(jq -r .path "$WORK/f.json")
  echo "   ep $EP: $(jq -r '.quality.quality.name+" v"+(.quality.revision.version|tostring)+" score="+(.customFormatScore|tostring)' "$WORK/f.json")"
  echo "           subs=[$(probe "$OLD_PATH" | subs_of)]  $OLD_PATH"
done

echo
[ "$FAIL" = 1 ] && { echo "PREFLIGHT EN ROJO — no se fuerza nada."; exit 2; }
[ "$APPLY" = 0 ] && { echo "Preflight limpio. Relanza con --apply para forzar el import."; exit 0; }

CMD=$(jq -n --argjson mi "$(cat "$WORK/mi.json")" --arg dl "$DL_ID" '{
  name:"ManualImport", importMode:"move",
  files:[{path:$mi.path, seriesId:$mi.series.id, episodeIds:[$mi.episodes[].id],
          quality:$mi.quality, languages:$mi.languages,
          releaseGroup:$mi.releaseGroup, downloadId:$dl}]}')
api -H 'Content-Type: application/json' -X POST "$SONARR/command" -d "$CMD" \
  | jq -r '"ManualImport cmdId="+(.id|tostring)+" "+.status'
