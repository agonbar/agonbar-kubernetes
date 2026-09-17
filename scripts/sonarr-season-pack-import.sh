#!/usr/bin/env bash
# Importa un pack de temporada cuando Sonarr mapea sus ficheros a la temporada
# equivocada. Con numeración absoluta de anime ("[Erai-raws] danmachi4 - 01")
# Sonarr parsea bien el número de episodio y mal la temporada: el fichero de la
# S4E01 acaba apuntando a la S1E01, que ya tiene fichero, así que el import
# automático se queda en importBlocked para siempre.
#
#   ./sonarr-season-pack-import.sh "/downloads/complete/<pack>" 429 4
#   ./sonarr-season-pack-import.sh "/downloads/complete/<pack>" 429 4 --apply
#   ONLY='S04E48' ./sonarr-season-pack-import.sh /downloads/complete 388 17
#
# Conserva el número de episodio que Sonarr ya parsea y solo cambia la temporada.
# ONLY filtra los candidatos de la carpeta por regex, para tratar un fichero
# suelto sin arrastrar a sus vecinos.
# Antes de tocar nada corre los 4 checks de
# runbooks/sonarr-manual-import-safety-checks.md por fichero, porque forzar un
# ManualImport con episodeIds explícitos se salta el perfil de calidad Y la
# blocklist. Un solo check en rojo aborta el pack entero.
#
# Complementa a sonarr-manual-import-preflight.sh, que solo mira un fichero.
set -euo pipefail

CTX="${KUBE_CTX:-lamg}"
NS="${KUBE_NS:-piracy}"
FOLDER="${1:?uso: $0 <carpeta-del-pack> <seriesId> <temporada> [--apply]}"
SERIES_ID="${2:?falta el seriesId}"
SEASON="${3:?falta la temporada de destino}"
APPLY=0; [ "${4:-}" = "--apply" ] && APPLY=1
# copy, no move: el torrent sigue sembrando en qBittorrent.
IMPORT_MODE="${IMPORT_MODE:-copy}"
# Sobrescribir un episodio que ya tiene fichero es justo el accidente del
# runbook, así que hay que pedirlo a mano.
REPLACE="${REPLACE:-0}"
ONLY="${ONLY:-.}"

K="kubectl --context=$CTX -n $NS"
WORK=$(mktemp -d); trap 'rm -rf "$WORK"; [ -n "${PF_PID:-}" ] && kill "$PF_PID" 2>/dev/null || true' EXIT

SONARR_KEY=$($K exec deploy/sonarr-deployment -c sonarr -- \
  sh -c 'grep -o "<ApiKey>[^<]*" /config/config.xml | cut -c9-')
$K port-forward svc/sonarr 18989:8989 >/dev/null 2>&1 & PF_PID=$!
sleep 3
SONARR="http://localhost:18989/api/v3"
api() { curl -sf -m 180 -H "X-Api-Key: $SONARR_KEY" "$@"; }

# ffprobe no está en sonarr; cruncharr monta /downloads y lo trae en /usr/bin.
subs_of() {
  $K exec deploy/cruncharr-deployment -c cruncharr -- \
    ffprobe -v error -show_entries stream=codec_type:stream_tags=language -of json "$1" 2>/dev/null \
    | jq -r '[.streams[]|select(.codec_type=="subtitle")|.tags.language//"und"]|join(",")'
}

FAIL=0
red()  { echo "  FALLA  $*"; FAIL=1; }
ok()   { echo "  ok     $*"; }
warn() { echo "  AVISO  $*"; }

ENC=$(jq -rn --arg s "$FOLDER" '$s|@uri')
api "$SONARR/manualimport?folder=$ENC&filterExistingFiles=false" > "$WORK/mi.json"
api "$SONARR/episode?seriesId=$SERIES_ID&seasonNumber=$SEASON" > "$WORK/eps.json"
PROFILE_ID=$(api "$SONARR/series/$SERIES_ID" | jq -r .qualityProfileId)
MIN_SCORE=$(api "$SONARR/qualityprofile/$PROFILE_ID" | jq -r '.minFormatScore')

echo "== $FOLDER"
echo "   destino: serie $SERIES_ID temporada $SEASON, perfil $PROFILE_ID (minFormatScore=$MIN_SCORE)"

# Reapunta cada fichero: mismo número de episodio, temporada de destino.
# Cuando Sonarr no mapea nada ("Invalid season or episode", porque el release
# viene numerado por arco) se cae al SxxEyy del nombre, que trae el número de
# episodio bueno con la temporada mala. Solo ese patrón: un [049ED216] suelto
# no vale como número de episodio.
jq --argjson sid "$SERIES_ID" --arg only "$ONLY" --slurpfile eps "$WORK/eps.json" '
  def nums_of:
    if (.episodes|length) > 0 then [.episodes[].episodeNumber]
    else [ (.path|split("/")|last)
           | match("[Ss][0-9]{1,2}[Ee]([0-9]{1,3})").captures[0].string | tonumber ]
    end;
  ($eps[0] | map({key: (.episodeNumber|tostring), value: .}) | from_entries) as $byNum
  # Algunos releases numeran por absoluto ("S16E50" en una serie de 5 temporadas).
  # Ambos mapas son solo de la temporada pedida, así que el fallback no puede
  # colarse a otra.
  | ($eps[0] | map(select(.absoluteEpisodeNumber != null)
               | {key: (.absoluteEpisodeNumber|tostring), value: .}) | from_entries) as $byAbs
  | [ .[] | select(.series.id == $sid) | select(.path|test($only))
      | . as $f | (nums_of) as $nums | select(($nums|length) > 0)
      | {
          path: .path,
          name: (.path|split("/")|last),
          score: .customFormatScore,
          quality: .quality,
          languages: .languages,
          releaseGroup: .releaseGroup,
          nums: $nums,
          targets: [$nums[] | ($byNum[tostring] // $byAbs[tostring] // null)]
        } ]
  | sort_by(.nums[0])' "$WORK/mi.json" > "$WORK/plan.json"

SKIPPED=$(jq --argjson sid "$SERIES_ID" --arg only "$ONLY" --slurpfile plan "$WORK/plan.json" '
  ($plan[0] | map(.path)) as $taken
  | [.[] | select(.series.id == $sid) | select(.path|test($only))
     | select(.path as $p | $taken | index($p) | not) | (.path|split("/")|last)]' "$WORK/mi.json")
if [ "$(echo "$SKIPPED" | jq length)" -gt 0 ]; then
  echo "   sin mapear (se quedan fuera):"
  echo "$SKIPPED" | jq -r '.[] | "         " + .'
fi

N=$(jq length "$WORK/plan.json")
[ "$N" -eq 0 ] && { echo "Ningún fichero de este pack mapea a la serie $SERIES_ID."; exit 1; }
echo

echo "1) blocklist: ¿está este release ya blocklisteado?"
REL=$(basename "$FOLDER")
BL=$(api "$SONARR/blocklist?page=1&pageSize=500&sortKey=date&sortDirection=descending" \
     | jq --argjson s "$SERIES_ID" '[.records[]|select(.seriesId==$s)]')
BL_HIT=$(echo "$BL" | jq --arg r "$REL" '[.[]|select(.sourceTitle==$r)]|length')
BL_N=$(echo "$BL" | jq length)
if [ "$BL_HIT" -gt 0 ]; then red "este release está en la blocklist ($BL_HIT entradas) — no se importa"
elif [ "$BL_N" -gt 0 ]; then warn "$BL_N entradas de la serie en la blocklist, ninguna es este release"
else ok "sin entradas para la serie"; fi

echo "2) custom format score contra el perfil"
LOW=$(jq --argjson m "$MIN_SCORE" '[.[]|select(.score == null or .score < $m)]|length' "$WORK/plan.json")
RANGE=$(jq -r '[.[].score]|"min=\(min) max=\(max)"' "$WORK/plan.json")
if [ "$LOW" -gt 0 ]; then red "$LOW ficheros por debajo de minFormatScore $MIN_SCORE ($RANGE)"
else ok "$RANGE, todos >= $MIN_SCORE"; fi

echo "3) episodios de destino"
MISSING=$(jq '[.[]|select(.targets|any(. == null))]|length' "$WORK/plan.json")
OCCUPIED=$(jq '[.[]|select(.targets|any(.hasFile))]|length' "$WORK/plan.json")
if [ "$MISSING" -gt 0 ]; then red "$MISSING ficheros apuntan a un episodio que no existe en la S$SEASON"
else ok "los $N ficheros mapean a episodios reales de la S$SEASON"; fi
if [ "$OCCUPIED" -gt 0 ] && [ "$REPLACE" != "1" ]; then
  red "$OCCUPIED destinos ya tienen fichero — relanza con REPLACE=1 si de verdad quieres sustituirlos"
elif [ "$OCCUPIED" -gt 0 ]; then warn "$OCCUPIED destinos ya tienen fichero y se sustituyen (REPLACE=1)"
else ok "ningún destino tiene fichero, no se sustituye nada"; fi

echo "4) pistas reales de cada fichero (ffprobe, no mediaInfo)"
NOSUBS=0
while IFS=$'\t' read -r P NAME; do
  S=$(subs_of "$P")
  [ -z "$S" ] && { NOSUBS=$((NOSUBS+1)); echo "         SIN SUBS  $NAME"; }
done < <(jq -r '.[]|[.path,.name]|@tsv' "$WORK/plan.json")
if [ "$NOSUBS" -gt 0 ]; then red "$NOSUBS ficheros sin ninguna pista de subtítulos"
else ok "los $N ficheros traen subtítulos (muestra: $(subs_of "$(jq -r '.[0].path' "$WORK/plan.json")"))"; fi

echo "5) recycleBin"
RB=$(api "$SONARR/config/mediamanagement" | jq -r '.recycleBin')
if [ -z "$RB" ] || [ "$RB" = "null" ]; then warn "sin configurar — lo que se sustituya se borra sin vuelta atrás"
else ok "configurado en $RB"; fi

echo
echo "se importaría:"
jq -r --argjson s "$SEASON" '.[] | "   \(.name[0:52])  ->  S\($s)E\(.targets|map(.episodeNumber|tostring)|join(",")) (id \(.targets|map(.id|tostring)|join(","))) score=\(.score)"' "$WORK/plan.json"

echo
[ "$FAIL" = 1 ] && { echo "PREFLIGHT EN ROJO — no se importa nada."; exit 2; }
[ "$APPLY" = 0 ] && { echo "Preflight limpio. Relanza con --apply para forzar el import."; exit 0; }

# Enlaza con la fila de cola del pack si existe, para que Sonarr la cierre sola.
DL_ID=$(api "$SONARR/queue?page=1&pageSize=1000&includeUnknownSeriesItems=true" \
  | jq -r --arg f "$FOLDER" '[.records[]|select(.outputPath == $f)][0].downloadId // empty')
[ -n "$DL_ID" ] && echo "downloadId del pack: $DL_ID"

CMD=$(jq -n --slurpfile plan "$WORK/plan.json" --arg dl "$DL_ID" --arg mode "$IMPORT_MODE" '{
  name: "ManualImport", importMode: $mode,
  files: [ $plan[0][] | {
    path, seriesId: '"$SERIES_ID"', episodeIds: [.targets[].id],
    quality, languages, releaseGroup
  } + (if $dl == "" then {} else {downloadId: $dl} end) ]}')

api -H 'Content-Type: application/json' -X POST "$SONARR/command" -d "$CMD" \
  | jq -r '"ManualImport cmdId=" + (.id|tostring) + " " + .status'
