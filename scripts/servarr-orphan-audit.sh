#!/usr/bin/env bash
# Audit Sonarr/Radarr/Lidarr for indexers and download clients that are enabled
# but point at something dead, and attribute grabs per indexer.
#
# An indexer earns its place by grabs, not by existing. A download client whose
# Deployment is scaled to 0 produces a permanent health error in every arr.
#
#   ./servarr-orphan-audit.sh
#
# Exits 1 if any arr reports a health error.
set -euo pipefail

CTX="${KUBE_CONTEXT:-lamg}"
NS="${SERVARR_NS:-piracy}"
DOMAIN="${SERVARR_DOMAIN:-adriangonzalezbarbosa.eu}"
PAGE="${PAGE:-1000}"
rc=0

key() {
  kubectl --context "$CTX" -n "$NS" exec "deploy/$1-deployment" -- \
    sh -c "sed -n 's|.*<ApiKey>\(.*\)</ApiKey>.*|\1|p' /config/config.xml" 2>/dev/null | tr -d '\r'
}

# Deployments scaled to 0 are parked on purpose, but the arrs keep calling them.
parked=$(kubectl --context "$CTX" -n "$NS" get deploy -o json \
  | jq -r '.items[] | select((.spec.replicas // 0) == 0) | .metadata.name | sub("-deployment$";"")' | sort)
echo "== deployments parked at 0 replicas =="
echo "${parked:-none}" | tr '\n' ' '; echo; echo

for app in sonarr radarr lidarr; do
  k=$(key "$app") || continue
  [[ -n "$k" ]] || { echo "== $app: no API key =="; continue; }
  v=v3; [[ "$app" == lidarr ]] && v=v1
  base="https://$app.$DOMAIN/api/$v"
  api() { curl -sf -H "X-Api-Key: $k" --max-time 45 "$base/$1"; }

  echo "== $app =="
  if ! api system/status >/dev/null 2>&1; then echo "  unreachable"; echo; continue; fi

  # Grabs per indexer over whatever the history page covers. Names here are the
  # arr's own labels, so "(Prowlarr)" marks the synced ones and bare names are
  # local indexers Prowlarr does not manage.
  hist=$(api "history?page=1&pageSize=$PAGE&eventType=1" || echo '{}')
  echo "  grabs by indexer  ($(echo "$hist" | jq -r '[.records[].date] | if length==0 then "no history" else "\(min[0:10]) .. \(max[0:10]), n=\(length)" end'))"
  echo "$hist" | jq -r '.records[] | .data.indexer // "?"' | sort | uniq -c | sort -rn | sed 's/^/    /'

  echo "  indexers with zero grabs in that window:"
  comm -23 \
    <(api indexer | jq -r '.[] | select(.enableRss or .enableAutomaticSearch or .enableInteractiveSearch) | .name' | sort -u) \
    <(echo "$hist" | jq -r '.records[] | .data.indexer // empty' | sort -u) \
    | sed 's/^/    /' || true

  echo "  download clients pointing at a parked deployment:"
  api downloadclient | jq -r --arg parked "$parked" --arg ns "$NS" '
    ($parked | split("\n") | map(select(. != ""))) as $P |
    .[] | select(.enable) |
    ((.fields[]? | select(.name == "host") | .value) // "") as $h |
    ($P | map(select($h == . or $h == (. + "." + $ns))) | first) as $hit |
    select($hit != null) |
    "    \(.name) -> \($h) (enabled, backend at 0 replicas)"' || true

  echo "  health:"
  api health | jq -r '.[] | "    \(.type)\t\(.source)\t\(.message)"' | column -t -s$'\t' | cut -c1-170
  api health | jq -e 'any(.type == "error") | not' >/dev/null || rc=1
  echo
done

exit $rc
