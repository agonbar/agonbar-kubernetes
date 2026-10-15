#!/usr/bin/env bash
# Rellena los huecos de una serie de Sonarr con ficheros que ya están en nas03, sin
# tocar ningún episodio que ya tenga fichero.
#
# Hecho para Detective Conan (seriesId 167) desde la carpeta de Syncthing de Iván, donde
# los nombres ya vienen en numeración TVDB ("Detective Conan - 7x03 - ...", "0x21" para
# los especiales). En nas03 solo se lee: nas02 tira por el tailnet con una clave limitada
# a `rrsync -ro`, a un staging dentro del mismo dataset que /tv, y Sonarr importa con
# ManualImport en modo move, que ahí es un rename y no una segunda copia.
#
#   ./sonarr-fill-gaps-from-nas03.sh plan     # cruza ficheros y episodios: huecos, solapes, títulos dudosos
#   ./sonarr-fill-gaps-from-nas03.sh copy     # da la clave en nas03 y lanza el rsync en nas02 (setsid)
#   ./sonarr-fill-gaps-from-nas03.sh status   # rsync vivo, GB copiados de los previstos
#   ./sonarr-fill-gaps-from-nas03.sh import   # ManualImport de lo ya copiado, solo a episodios sin fichero
#   DRY=1 ./sonarr-fill-gaps-from-nas03.sh import  # lo mismo, pero solo enseña qué haría
#   TRUST_PLAN=1 ... import  # si Sonarr parsea otro episodio, usa el del plan (solo a huecos)
#   ./sonarr-fill-gaps-from-nas03.sh revoke   # quita la clave de nas03, borra el staging si quedó vacío
#
# import se puede repetir mientras copia: rsync escribe cada fichero en un temporal oculto
# y solo lo renombra al terminar, e import solo acepta rutas que estén en el plan. Antes
# de importar vuelve a mirar hasFile, así que no reemplaza nada aunque Cruncharr haya
# traído el episodio entretanto.
#
# ManualImport con episodeIds explícitos se salta el perfil de calidad
# (runbooks/sonarr-manual-import-safety-checks.md). Aquí es lo que queremos, porque un SD
# es mejor que un hueco y no hay nada que pisar. Pero el perfil sigue con upgrades, así
# que Sonarr puede sustituir más tarde un fichero de estos por uno mejor, y sin
# recycleBin el viejo se borra. El original sigue en nas03.
set -euo pipefail

SERIES_ID=${SERIES_ID:-167}
SRC=${SRC:-/mnt/RAID/ivan/ivan-1}             # en nas03, solo lectura
N03_TS=${N03_TS:-100.72.0.140}                # nas03 en el tailnet, visto desde nas02
STAGE=/mnt/RAID/docker/media/tv/_import-$SERIES_ID  # en nas02; Sonarr lo ve como /tv/...
STAGE_SONARR=/tv/_import-$SERIES_ID
TAG=fill-gaps-$SERIES_ID
STATE=${STATE:-$HOME/.cache/sonarr-fill-gaps/$SERIES_ID}
BWLIMIT=${BWLIMIT:-0}                         # KiB/s para rsync, 0 = sin límite
K="kubectl --context=${KUBE_CTX:-lamg} -n ${KUBE_NS:-piracy}"
N02=(ssh -o BatchMode=yes -o ConnectTimeout=10 nas02)
ALIVE="{ [ -f ~/$TAG.pid ] && kill -0 \$(cat ~/$TAG.pid) 2>/dev/null; }"  # se evalúa en nas02
N03=(ssh -o BatchMode=yes -o ConnectTimeout=10 nas03)

log(){ echo "[$(date +%H:%M:%S)] $*"; }
die(){ echo "FATAL: $*" >&2; exit 1; }
mkdir -p "$STATE"

sonarr_up() {
  SONARR_KEY=$($K exec deploy/sonarr-deployment -c sonarr -- \
    sh -c 'grep -o "<ApiKey>[^<]*" /config/config.xml | cut -c9-')
  $K port-forward svc/sonarr 18989:8989 >/dev/null 2>&1 & PF_PID=$!
  trap 'kill $PF_PID 2>/dev/null || true' EXIT
  for _ in $(seq 1 20); do curl -sf -m 2 -o /dev/null http://localhost:18989/ping && return; sleep 0.5; done
  die "port-forward a sonarr no responde"
}
api(){ curl -sf -m 600 -H "X-Api-Key: $SONARR_KEY" "$@"; }
SONARR=http://localhost:18989/api/v3

plan() {
  sonarr_up
  api "$SONARR/episode?seriesId=$SERIES_ID" > "$STATE/eps.json"
  "${N03[@]}" "cd $SRC && find . -type f -printf '%s\t%P\n'" > "$STATE/files.tsv"
  python3 -I - "$STATE" <<'EOF'
import collections, difflib, json, re, sys
st = sys.argv[1]
eps = {(e["seasonNumber"], e["episodeNumber"]): e for e in json.load(open(f"{st}/eps.json"))}
pat = re.compile(r"(?:^|[ ._-])(?:(\d+)[xX](\d+)|[sS](\d+)[eE](\d+))(?:[ ._-]|$)")
words = lambda t: " ".join(re.sub(r"[^a-z0-9 ]+", " ", (t or "").lower().replace("parte", "part")).split())
plan, other = [], collections.Counter()
for line in open(f"{st}/files.tsv"):
    size, path = line.rstrip("\n").split("\t", 1)
    name = path.rsplit("/", 1)[-1]
    m = pat.search(name)
    if not name.lower().endswith((".mkv", ".mp4", ".avi")) or not m:
        other["sin SxE o no es vídeo"] += 1; continue
    s, e = int(m[1] or m[3]), int(m[2] or m[4])
    ep = eps.get((s, e))
    if not ep:
        other["no existe en TVDB"] += 1; print("  no existe en TVDB:", path); continue
    title = re.sub(r" \[[^\]]*\]$", "", name.rsplit(".", 1)[0][m.end():].strip(" -"))
    plan.append(dict(path=path, size=int(size), s=s, e=e, id=ep["id"], has=ep["hasFile"],
                     ratio=round(difflib.SequenceMatcher(None, words(title), words(ep["title"])).ratio(), 2),
                     title=title, tvdb=ep["title"]))
gaps = [p for p in plan if not p["has"]]
json.dump(gaps, open(f"{st}/plan.json", "w"))
open(f"{st}/files.txt", "w").write("".join(p["path"] + "\n" for p in gaps))
dup = [k for k, n in collections.Counter((p["s"], p["e"]) for p in gaps).items() if n > 1]
print(f"huecos a copiar: {len(gaps)} ({sum(p['size'] for p in gaps)/1e9:.0f} GB)")
print(f"ya tienen fichero en Sonarr, se saltan: {len(plan)-len(gaps)}")
for k, n in other.items(): print(f"ignorados, {k}: {n}")
if dup: print("AVISO: varios ficheros para el mismo episodio:", dup)
low = sorted((p for p in gaps if p["ratio"] < 0.5), key=lambda p: p["ratio"])
print(f"huecos con título poco parecido al de TVDB ({len(low)}), revísalos:")
for p in low: print(f"  S{p['s']:02}E{p['e']:02} {p['ratio']:.2f}  '{p['title'][:50]}'  vs  '{(p['tvdb'] or '')[:50]}'")
EOF
}

copy() {
  [ -s "$STATE/files.txt" ] || die "falta el plan: ./$(basename "$0") plan"
  "${N02[@]}" "mkdir -p $STAGE && { [ -f ~/.ssh/$TAG ] || ssh-keygen -q -t ed25519 -N '' -C $TAG@nas02 -f ~/.ssh/$TAG; }"
  local pub; pub=$("${N02[@]}" "cat ~/.ssh/$TAG.pub")
  "${N03[@]}" "python3 - $(printf %q "$pub") $(printf %q "$SRC")" <<'EOF'
import json, subprocess, sys
key, src = sys.argv[1], sys.argv[2]
u = json.loads(subprocess.check_output(["midclt", "call", "user.query", json.dumps([["username", "=", "truenas_admin"]])]))[0]
keys = [k for k in (u["sshpubkey"] or "").splitlines() if k.strip() and key not in k]
keys.append(f'command="/usr/bin/rrsync -ro {src}",restrict {key}')
subprocess.check_call(["midclt", "call", "user.update", str(u["id"]), json.dumps({"sshpubkey": "\n".join(keys)})],
                      stdout=subprocess.DEVNULL)
print(f"nas03: {len(keys)} claves autorizadas, la de nas02 limitada a rrsync -ro {src}")
EOF
  "${N02[@]}" "cat > ~/$TAG.files" < "$STATE/files.txt"
  # pgrep -f no sirve: el propio comando de ssh contiene "rsync" y el tag, y se encuentra a sí mismo.
  "${N02[@]}" "$ALIVE && echo 'rsync ya corriendo' || { \
    nohup rsync -rt --files-from=\$HOME/$TAG.files --bwlimit=$BWLIMIT \
      -e \"ssh -i \$HOME/.ssh/$TAG -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30\" \
      truenas_admin@$N03_TS:./ $STAGE/ > ~/$TAG.log 2>&1 < /dev/null & echo \$! > ~/$TAG.pid; }"
  log "rsync lanzado en nas02 -> $STAGE ($(wc -l < "$STATE/files.txt") ficheros)"
}

status() {
  local want; want=$(python3 -c "import json;print(sum(p['size'] for p in json.load(open('$STATE/plan.json'))))")
  "${N02[@]}" "$ALIVE && echo 'rsync: corriendo' || echo \"rsync: parado, salida \$(tail -1 ~/$TAG.log 2>/dev/null)\"; \
    tail -3 ~/$TAG.log 2>/dev/null; \
    n=\$(find $STAGE -type f -name '*.mkv' -o -type f -name '*.mp4' -o -type f -name '*.avi' 2>/dev/null | wc -l); \
    b=\$(du -sb --apparent-size $STAGE 2>/dev/null | cut -f1); \
    echo \"staging: \$n ficheros, \$((\${b:-0}/1000000000)) GB de $((want/1000000000)) GB\""
}

import() {
  [ -s "$STATE/plan.json" ] || die "falta el plan"
  sonarr_up
  local dir fail=0
  # Carpeta a carpeta, consultar, decidir e importar, para que un cuelgue de Sonarr en una
  # (pasó una vez, más de 600 s en una carpeta que luego respondió en 9) no tire el resto.
  while IFS= read -r dir; do import_dir "$dir" || { fail=1; echo "  FALLA $dir, sigo con la siguiente"; }
  done < <("${N02[@]}" "cd $STAGE && find . -mindepth 1 -maxdepth 1 -type d ! -name '.*' -printf '$STAGE_SONARR/%P\n'")
  api "$SONARR/series/$SERIES_ID" | jq -r '"Sonarr ahora: \(.statistics.episodeFileCount) ficheros de \(.statistics.totalEpisodeCount) episodios"'
  [ "$fail" = 0 ] || die "alguna carpeta falló; vuelve a lanzar import, lo ya importado no se repite"
}

import_dir() {
  local dir=$1 try
  # Sin seriesId: con él, Sonarr ignora folder y devuelve los ficheros que ya tiene la serie.
  for try in 1 2 3; do
    curl -sSf -m 300 -H "X-Api-Key: $SONARR_KEY" \
      "$SONARR/manualimport?folder=$(jq -rn --arg s "$dir" '$s|@uri')&filterExistingFiles=false" > "$STATE/mi.json" && break
    [ "$try" = 3 ] && return 1
  done
  api "$SONARR/episode?seriesId=$SERIES_ID" > "$STATE/eps-now.json"
  # Sonarr a veces lee "28x01 - Titulo (Prologue)" como S1E28; con TRUST_PLAN=1 manda el plan
  # (SxE del nombre, cotejado con TVDB en `plan`), pero solo hacia un episodio sin fichero.
  jq --slurpfile plan "$STATE/plan.json" --slurpfile eps "$STATE/eps-now.json" --arg root "$STAGE_SONARR/" \
     --arg trust "${TRUST_PLAN:-0}" '
    ($plan[0] | map({key: .path, value: .id}) | from_entries) as $want
    | ($eps[0] | map({key: (.id|tostring), value: .hasFile}) | from_entries) as $has
    | map(. as $i | ($i.path | ltrimstr($root)) as $rel
      | {item: $i, why: (
          if $want[$rel] == null then "no está en el plan"
          elif $has[$want[$rel]|tostring] then "el episodio ya tiene fichero"
          elif ([$i.episodes[]?.id] == [$want[$rel]]) then "ok"
          elif $trust == "1" then "ok"
          else "Sonarr lo asigna a otro episodio" end)}
      | if .why == "ok" then .item.episodes = [{id: $want[$rel]}] else . end)' "$STATE/mi.json" > "$STATE/decision.json"
  echo "${dir#"$STAGE_SONARR/"}: $(jq -r '[group_by(.why)[] | "\(.[0].why) \(length)"] | join(", ")' "$STATE/decision.json"); \
$(jq -r '[.[] | select(.why=="ok") | .item | "\(.quality.quality.name) \([.languages[]?.name]|join("+"))"] | group_by(.) | map("\(.[0]) x\(length)") | join(", ")' "$STATE/decision.json")"
  jq -r '.[] | select(.why != "ok" and .why != "el episodio ya tiene fichero") | "  \(.why): \(.item.path)"' "$STATE/decision.json" | head -5
  [ "${DRY:-0}" = 1 ] && return
  local n; n=$(jq '[.[] | select(.why=="ok")] | length' "$STATE/decision.json")
  [ "$n" -gt 0 ] || return 0
  for off in $(seq 0 50 $((n - 1))); do
    jq -c --argjson off "$off" --argjson sid "$SERIES_ID" '{name: "ManualImport", importMode: "move",
      files: [[.[] | select(.why=="ok") | .item][$off:$off+50][] | {path, seriesId: $sid, episodeIds: [.episodes[].id],
        quality, languages, releaseGroup, indexerFlags, releaseType}]}' "$STATE/decision.json" > "$STATE/cmd.json"
    local id st; id=$(api -H 'Content-Type: application/json' -X POST "$SONARR/command" -d @"$STATE/cmd.json" | jq -r .id)
    for _ in $(seq 1 360); do
      st=$(api "$SONARR/command/$id" | jq -r .status)
      { [ "$st" = completed ] || [ "$st" = failed ]; } && break; sleep 5
    done
    echo "  ManualImport $((off + 1))-$((off + 50 < n ? off + 50 : n)) de $n: $st"
    [ "$st" = completed ] || return 1
  done
}

revoke() {
  local pub; pub=$("${N02[@]}" "cat ~/.ssh/$TAG.pub 2>/dev/null" || true)
  if [ -n "$pub" ]; then
    "${N03[@]}" "python3 - $(printf %q "$pub")" <<'EOF'
import json, subprocess, sys
key = sys.argv[1]
u = json.loads(subprocess.check_output(["midclt", "call", "user.query", json.dumps([["username", "=", "truenas_admin"]])]))[0]
keys = [k for k in (u["sshpubkey"] or "").splitlines() if k.strip() and key not in k]
subprocess.check_call(["midclt", "call", "user.update", str(u["id"]), json.dumps({"sshpubkey": "\n".join(keys)})],
                      stdout=subprocess.DEVNULL)
print(f"nas03: clave de nas02 quitada, quedan {len(keys)}")
EOF
  fi
  "${N02[@]}" "$ALIVE && echo 'AVISO: el rsync sigue corriendo'; \
    rm -f ~/.ssh/$TAG ~/.ssh/$TAG.pub ~/$TAG.files ~/$TAG.pid ~/$TAG.log; find $STAGE -depth -type d -empty -delete 2>/dev/null; \
    [ -d $STAGE ] && echo \"staging no vacío: \$(find $STAGE -type f | wc -l) ficheros en $STAGE\" || echo 'staging borrado'"
}

case "${1:-}" in
  plan|copy|status|import|revoke) "$1" ;;
  *) sed -n '2,29p' "$0"; exit 1 ;;
esac
