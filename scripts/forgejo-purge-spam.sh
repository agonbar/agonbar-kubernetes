#!/usr/bin/env bash
# Purge self-registration spam accounts from the personal Forgejo.
#
# Self-registration was left open and the instance accumulated ~21.5k throwaway
# accounts pushing ~5k SEO spam repositories. Registration is closed as of
# Forgejo 16.0.3 (FORGEJO__service__DISABLE_REGISTRATION), so this only has to
# clean up the backlog.
#
#   list    read-only SQL against the Forgejo sqlite DB, writes the candidate
#           usernames to a file and prints a summary. Changes nothing.
#   purge   deletes each candidate through the admin API with purge=true, which
#           removes their repositories too. Resumable: already-deleted users
#           come back 404 and are skipped.
#
# Everything created before CUTOFF is kept, as is every name in KEEP. Widen KEEP
# rather than hand-editing the candidate file, so a rerun stays reproducible.

set -euo pipefail

CONTEXT="${CONTEXT:-lamg}"
NAMESPACE="${NAMESPACE:-agonbar}"
SELECTOR="${SELECTOR:-app=gitea}"
BASE_URL="${BASE_URL:-https://gitea.adriangonzalezbarbosa.eu}"
CUTOFF="${CUTOFF:-2026-01-01}"
CANDIDATES="${CANDIDATES:-/tmp/forgejo-spam-candidates.txt}"
PARALLEL="${PARALLEL:-8}"

# Accounts that predate the spam wave and must survive regardless of CUTOFF.
KEEP="agonbar asdasd chismo martin francisco coinscrap-ci oscar alejandro amy luchipuchi test coinscrap tests"

# Accounts that fall regardless of CUTOFF. These registered in 2025, before the
# flood, but they are the same operation: disposable throwaway domains, one
# keyword repository or none at all.
DROP="mejoraburo seguroscolombia payavideo signicolor maxwarehouse boletosetnl nuevapasion brandwatch"

pod() {
  kubectl --context "$CONTEXT" get pods -n "$NAMESPACE" -l "$SELECTOR" \
    --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}'
}

# Users are matched case-insensitively; Forgejo stores the canonical form in
# lower_name, which is also what the admin API expects in the URL.
list_sql() {
  local first=1 name
  for name in $1; do
    [ $first -eq 1 ] && first=0 || printf ','
    printf "'%s'" "$name"
  done
}
keep_sql() { list_sql "$KEEP"; }
drop_sql() { list_sql "$DROP"; }

cmd_list() {
  local p doomed kept
  p="$(pod)"
  echo "pod=$p cutoff=$CUTOFF" >&2

  # An account falls if it registered after CUTOFF or is named in DROP, unless
  # KEEP protects it. KEEP wins over both, so a name in KEEP is never deleted.
  doomed="(u.created_unix >= strftime('%s','$CUTOFF') OR u.lower_name IN ($(drop_sql)))
          AND u.lower_name NOT IN ($(keep_sql))"
  kept="NOT ($doomed)"

  kubectl --context "$CONTEXT" exec -i -n "$NAMESPACE" "$p" -- \
    sqlite3 "file:/data/gitea/gitea.db?mode=ro" <<SQL > "$CANDIDATES"
.mode list
SELECT u.lower_name FROM user u WHERE $doomed ORDER BY u.id;
SQL

  local n; n=$(wc -l < "$CANDIDATES")
  echo "candidatos: $n -> $CANDIDATES" >&2

  kubectl --context "$CONTEXT" exec -i -n "$NAMESPACE" "$p" -- \
    sqlite3 "file:/data/gitea/gitea.db?mode=ro" <<SQL >&2
.mode list
SELECT 'usuarios totales:  ' || COUNT(*) FROM user WHERE type=0;
SELECT 'se conservan:      ' || COUNT(*) FROM user u WHERE $kept;
SELECT 'repos totales:     ' || COUNT(*) FROM repository;
SELECT 'repos que caen:    ' || COUNT(*) FROM repository r JOIN user u ON u.id=r.owner_id WHERE $doomed;
SELECT 'repos que quedan:  ' || COUNT(*) FROM repository r JOIN user u ON u.id=r.owner_id WHERE $kept;
SQL
}

delete_one() {
  local user="$1" code
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 \
    -X DELETE -H "Authorization: token $FORGEJO_TOKEN" \
    "$BASE_URL/api/v1/admin/users/$user?purge=true")
  case "$code" in
    204|404) ;;                                  # borrado, o ya no estaba
    *) echo "FALLO $code $user" >&2 ;;
  esac
}
export -f delete_one
export BASE_URL

cmd_purge() {
  : "${FORGEJO_TOKEN:?exporta FORGEJO_TOKEN con un token de admin}"
  [ -s "$CANDIDATES" ] || { echo "no hay $CANDIDATES, ejecuta 'list' antes" >&2; exit 1; }

  local n; n=$(wc -l < "$CANDIDATES")
  echo "borrando $n usuarios con purge=true, ${PARALLEL} en paralelo" >&2
  xargs -a "$CANDIDATES" -P "$PARALLEL" -n 1 -I{} bash -c 'delete_one "$@"' _ {}
  echo "hecho" >&2
}

case "${1:-}" in
  list)  cmd_list ;;
  purge) cmd_purge ;;
  *) echo "uso: $0 {list|purge}" >&2; exit 2 ;;
esac
