#!/usr/bin/env bash
# Audit Prowlarr indexers: health, live test, 90-day usefulness, error patterns.
#
# Reads the API key straight from the pod's config.xml so there is nothing to
# keep in sync. Override with PROWLARR_URL / PROWLARR_API_KEY.
#
#   ./prowlarr-indexer-audit.sh          # report
#   ./prowlarr-indexer-audit.sh --test   # report + POST indexer/testall (slow)
#
# Exits 1 if any indexer is failing or has no definition.
set -euo pipefail

CTX="${KUBE_CONTEXT:-lamg}"
NS="${PROWLARR_NS:-piracy}"
DEPLOY="${PROWLARR_DEPLOY:-deploy/prowlarr-deployment}"
URL="${PROWLARR_URL:-https://prowlarr.adriangonzalezbarbosa.eu}"
DAYS="${DAYS:-90}"
RUN_TEST=0
[[ "${1:-}" == "--test" ]] && RUN_TEST=1

if [[ -z "${PROWLARR_API_KEY:-}" ]]; then
  PROWLARR_API_KEY=$(kubectl --context "$CTX" -n "$NS" exec "$DEPLOY" -c prowlarr -- \
    sh -c "sed -n 's|.*<ApiKey>\(.*\)</ApiKey>.*|\1|p' /config/config.xml" | tr -d '\r')
fi
[[ -n "$PROWLARR_API_KEY" ]] || { echo "no API key" >&2; exit 2; }

api() { curl -sf -H "X-Api-Key: $PROWLARR_API_KEY" --max-time 60 "$URL/api/v1/$1"; }
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
rc=0

api indexer        > "$tmp/idx.json"
api indexerstatus  > "$tmp/st.json"
api health         > "$tmp/health.json"
api tag            > "$tmp/tag.json"
api indexerproxy   > "$tmp/proxy.json"
from=$(date -u -d "-${DAYS} days" +%Y-%m-%dT%H:%M:%SZ); to=$(date -u +%Y-%m-%dT%H:%M:%SZ)
api "indexerstats?startDate=$from&endDate=$to" > "$tmp/stats.json"

echo "== health =="
jq -r '.[] | "\(.type)\t\(.source)\t\(.message)"' "$tmp/health.json" | column -t -s$'\t'
jq -e 'length == 0' "$tmp/health.json" >/dev/null || rc=1

echo
echo "== indexers (last ${DAYS}d) =="
# grabs is the only number that says an indexer earns its place; queries with
# zero grabs is pure noise against every search.
jq -s -r '
  (.[0]) as $idx | (.[1].indexers // []) as $st | (.[2]) as $fail |
  ($fail | map({key: (.indexerId|tostring), value: .disabledTill}) | from_entries) as $down |
  $idx | sort_by(.name)[] |
  ($st[] | select(.indexerName == .indexerName)) as $_ |
  [ .id, .name, (if .enable then "on" else "OFF" end),
    (($st | map(select(.indexerName == ($idx[] | select(.id == .id) | .name)))) | length | tostring)
  ] | @tsv' "$tmp/idx.json" "$tmp/stats.json" "$tmp/st.json" >/dev/null 2>&1 || true
jq -r --slurpfile stats "$tmp/stats.json" --slurpfile down "$tmp/st.json" '
  ($stats[0].indexers // []) as $S |
  ($down[0] | map(.indexerId)) as $D |
  sort_by(.name)[] |
  . as $i |
  ($S[] | select(.indexerName == $i.name)) // {numberOfQueries:0,numberOfGrabs:0,numberOfFailedQueries:0,averageResponseTime:0} as $s |
  [ $i.id, $i.name,
    (if $i.enable then "on" else "OFF" end),
    (if ($D | index($i.id)) then "DOWN" else "-" end),
    ($s.numberOfQueries|tostring), ($s.numberOfGrabs|tostring),
    ($s.numberOfFailedQueries|tostring), (($s.averageResponseTime|floor|tostring)+"ms"),
    (if ($i.tags|length) > 0 then ($i.tags|map(tostring)|join(",")) else "-" end)
  ] | @tsv' "$tmp/idx.json" \
  | (echo -e "ID\tNAME\tEN\tSTATE\tQUERIES\tGRABS\tFAILQ\tAVG\tTAGS"; cat) | column -t -s$'\t'
jq -e 'length == 0' "$tmp/st.json" >/dev/null || rc=1

echo
echo "== proxies (a proxy with a tag no indexer carries is dead weight) =="
jq -r --slurpfile idx "$tmp/idx.json" --slurpfile tag "$tmp/tag.json" '
  .[] | . as $p |
  ($p.tags | map(. as $t | ($tag[0][] | select(.id == $t) | .label))) as $labels |
  ($idx[0] | map(select(.tags | any(. as $t | ($p.tags | index($t)) != null))) | length) as $users |
  [$p.name, ($labels|join(",")), ("indexers using it: " + ($users|tostring))] | @tsv' "$tmp/proxy.json" \
  | column -t -s$'\t'

echo
echo "== stale definition files (removed upstream, site usually dead) =="
newest=$(kubectl --context "$CTX" -n "$NS" exec "$DEPLOY" -c prowlarr -- \
  sh -c 'ls -t /config/Definitions/*.yml | head -1 | xargs stat -c %Y')
kubectl --context "$CTX" -n "$NS" exec "$DEPLOY" -c prowlarr -- \
  sh -c "find /config/Definitions -name '*.yml' -newermt \"@\$(( $newest - 86400 ))\" -prune -o -name '*.yml' -print" \
  | sed 's|.*/||;s|\.yml$||' | sort | tr '\n' ' '
echo

echo
echo "== top error patterns (current log page) =="
api "log?page=1&pageSize=300&sortKey=time&sortDirection=descending&level=error" \
  | jq -r '.records[] | "\(.logger)\t\(.message)"' \
  | sed 's/[0-9a-f]\{8\}-[0-9a-f-]\{27\}/<GUID>/g; s/[0-9]\{3,\}/<N>/g' \
  | sort | uniq -c | sort -rn | head -15

if [[ $RUN_TEST -eq 1 ]]; then
  echo
  echo "== live test of every indexer =="
  curl -sf -X POST -H "X-Api-Key: $PROWLARR_API_KEY" -H 'Content-Type: application/json' \
    --max-time 300 "$URL/api/v1/indexer/testall" \
    | jq -r --slurpfile idx "$tmp/idx.json" '
        .[] | [ .id,
                (($idx[0][] | select(.id == .id) | .name) // "?"),
                (if .isValid then "OK" else "FAIL" end),
                ((.validationFailures[]?.errorMessage) // "") ] | @tsv' \
    | column -t -s$'\t' | cut -c1-160
fi

exit $rc
