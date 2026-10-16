#!/usr/bin/env bash
# Pasa películas que Sonarr tiene como especiales (S00) de una serie a Radarr, en la
# carpeta de películas de anime, y solo entonces las quita de Sonarr.
#
# Hecho para las 25 películas de cine de Detective Conan que entraron como S00 desde la
# colección de Iván. El mapa es un TSV "episodeFileId<TAB>tmdbId<TAB>etiqueta", sacado
# cruzando los S00 de Sonarr con /movie/lookup de Radarr (año ±1, idioma japonés).
#
#   ./sonarr-movies-to-radarr.sh import MAP.tsv   # alta en Radarr + copia en nas02 + ManualImport
#   ./sonarr-movies-to-radarr.sh verify MAP.tsv   # qué tiene ya fichero en Radarr
#   ./sonarr-movies-to-radarr.sh purge  MAP.tsv   # borra de Sonarr solo las que Radarr ya tiene
#
# Las películas entran SIN monitorizar: Radarr no busca ni sustituye nada, así que un
# doblaje en castellano que solo tenga esta copia no se pierde por un "upgrade".
# La copia va por nas02 dentro del mismo dataset (RAID/docker), sin pasar por la red;
# purge borra el fichero de Sonarr, que sin recycleBin es definitivo, por eso exige
# que Radarr tenga el suyo.
set -euo pipefail

ROOT=${ROOT:-/movies}                                   # carpeta raíz de Radarr (anime)
PROFILE=${PROFILE:-1}
TV=/mnt/RAID/docker/media/tv                            # /tv de Sonarr en nas02
STAGE=/mnt/RAID/docker/media/movies/_import-from-sonarr # /movies/_import-from-sonarr en Radarr
STAGE_RADARR=$ROOT/_import-from-sonarr
K="kubectl --context=${KUBE_CTX:-lamg} -n ${KUBE_NS:-piracy}"
N02=(ssh -n -o BatchMode=yes -o ConnectTimeout=10 nas02)

log(){ echo "[$(date +%H:%M:%S)] $*"; }
key(){ $K exec "deploy/$1-deployment" -c "$1" -- sh -c 'grep -o "<ApiKey>[^<]*" /config/config.xml | cut -c9-' </dev/null; }
SK=$(key sonarr); RK=$(key radarr)
# El cuerpo va por stdin (exec -i): un -d en línea de comandos se rompe con comillas en títulos.
sonarr(){ $K exec -i deploy/sonarr-deployment -c sonarr -- curl -sf -m 600 -H "X-Api-Key: $SK" -H 'Content-Type: application/json' "$@"; }
radarr(){ $K exec -i deploy/radarr-deployment -c radarr -- curl -sf -m 600 -H "X-Api-Key: $RK" -H 'Content-Type: application/json' "$@"; }
MAP=${2:?falta el MAP.tsv}

movie_of(){ radarr "http://localhost:7878/api/v3/movie?tmdbId=$1" </dev/null | jq -c '.[0] // empty'; }

import() {
  "${N02[@]}" "mkdir -p '$STAGE'"
  while IFS=$'\t' read -r fid tmdb label; do
    local m; m=$(movie_of "$tmdb")
    if [ -z "$m" ]; then
      m=$(radarr "http://localhost:7878/api/v3/movie/lookup/tmdb?tmdbId=$tmdb" </dev/null |
        jq -c --arg r "$ROOT" --argjson p "$PROFILE" '. + {rootFolderPath: $r, qualityProfileId: $p, monitored: false,
          minimumAvailability: "released", addOptions: {searchForMovie: false, monitor: "none"}}' |
        radarr -X POST -d @- http://localhost:7878/api/v3/movie)
      log "alta: $(jq -r '"\(.title) (\(.year))"' <<<"$m")"
    fi
    [ "$(jq -r .hasFile <<<"$m")" = true ] && { log "ya tiene fichero: $label"; continue; }
    local rel; rel=$(sonarr "http://localhost:8989/api/v3/episodefile/$fid" </dev/null | jq -r .relativePath)
    local series; series=$(sonarr "http://localhost:8989/api/v3/episodefile/$fid" </dev/null | jq -r .seriesId)
    local spath; spath=$(sonarr "http://localhost:8989/api/v3/series/$series" </dev/null | jq -r '.path | ltrimstr("/tv/")')
    # Radarr tiene renameMovies=false y guarda el nombre con el que llega, así que el fichero
    # se copia ya con su formato: "{Movie Title} ({Release Year}) {Quality Full}".
    local quality; quality=$(sonarr "http://localhost:8989/api/v3/episodefile/$fid" </dev/null | jq -r .quality.quality.name)
    local dest; dest="$STAGE/$tmdb/$(jq -r '.path | split("/")[-1]' <<<"$m") $quality.mkv"
    "${N02[@]}" "mkdir -p $(printf %q "$STAGE/$tmdb") && { [ -f $(printf %q "$dest") ] || { cp $(printf %q "$TV/$spath/$rel") $(printf %q "$dest.part") && mv $(printf %q "$dest.part") $(printf %q "$dest"); }; }" ||
      echo "  FALLA la copia: $label"
  done < "$MAP"
  # Una sola consulta para toda la carpeta; el movieId lo pone el mapa, no el parseo del nombre.
  local items; items=$(radarr "http://localhost:7878/api/v3/manualimport?folder=$(jq -rn --arg s "$STAGE_RADARR" '$s|@uri')&filterExistingFiles=false" </dev/null)
  local files='[]'
  while IFS=$'\t' read -r fid tmdb label; do
    local id; id=$(movie_of "$tmdb" | jq -r '.id // empty')
    [ -n "$id" ] || continue
    files=$(jq -c --argjson f "$files" --arg pre "$STAGE_RADARR/$tmdb/" --argjson id "$id" \
      '$f + [.[] | select(.path | startswith($pre)) | {path, movieId: $id, quality, languages, releaseGroup, indexerFlags}]' <<<"$items")
  done < "$MAP"
  local n; n=$(jq length <<<"$files")
  [ "$n" -gt 0 ] || { log "nada que importar"; return; }
  local cmd; cmd=$(jq -c '{name: "ManualImport", importMode: "move", files: .}' <<<"$files" |
    radarr -X POST -d @- http://localhost:7878/api/v3/command | jq -r .id)
  local st=queued
  for _ in $(seq 1 120); do
    st=$(radarr "http://localhost:7878/api/v3/command/$cmd" </dev/null | jq -r .status)
    { [ "$st" = completed ] || [ "$st" = failed ]; } && break; sleep 5
  done
  log "ManualImport de $n ficheros: $st"
  verify
}

verify() {
  local ok=0 total=0
  while IFS=$'\t' read -r fid tmdb label; do
    total=$((total + 1))
    if [ "$(movie_of "$tmdb" | jq -r '.hasFile // false')" = true ]; then ok=$((ok + 1)); else echo "  sin fichero en Radarr: $label"; fi
  done < "$MAP"
  log "Radarr tiene fichero de $ok de $total"
}

purge() {
  local ids='[]'
  while IFS=$'\t' read -r fid tmdb label; do
    if [ "$(movie_of "$tmdb" | jq -r '.hasFile // false')" = true ]; then ids=$(jq -c --argjson i "$fid" '. + [$i]' <<<"$ids")
    else echo "  me la salto, Radarr aún no la tiene: $label"; fi
  done < "$MAP"
  [ "$(jq length <<<"$ids")" -gt 0 ] || { log "nada que borrar"; return; }
  jq -c '{episodeFileIds: .}' <<<"$ids" | sonarr -X DELETE -d @- http://localhost:8989/api/v3/episodefile/bulk >/dev/null
  log "borrados de Sonarr: $(jq length <<<"$ids") ficheros"
  "${N02[@]}" "find '$STAGE' -depth -type d -empty -delete 2>/dev/null; [ -d '$STAGE' ] && echo \"staging no vacío: \$(ls '$STAGE' | wc -l) ficheros\" || true"
}

case "${1:-}" in
  import|verify|purge) "$1" ;;
  *) sed -n '2,19p' "$0"; exit 1 ;;
esac
