#!/usr/bin/env bash
# Manage records in the adriangonzalezbarbosa.eu zone through the OVH API.
#
# Credentials come from dotfiles/nix/secrets/ovh-dns-adriangonzalezbarbosa.age
# (made by scripts/ovh-dns-token-wizard.sh) and are never printed. The token
# can only touch this zone.
#
# The zone has a wildcard pointing at ovh02. An explicit A record wins over it,
# which is how names like sure.<zone> resolve to a tailnet address instead.
#
#   scripts/ovh-dns.sh get sure             # list records for a subdomain
#   scripts/ovh-dns.sh set-a sure 100.72.0.132 [ttl]
#   scripts/ovh-dns.sh delete sure
set -euo pipefail

ZONE="adriangonzalezbarbosa.eu"
API="https://eu.api.ovh.com/1.0"
SECRETS_DIR="${SECRETS_DIR:-$HOME/dotfiles/nix/secrets}"

eval "$(cd "$SECRETS_DIR" && agenix -d ovh-dns-adriangonzalezbarbosa.age | grep -E '^OVH_[A-Z_]+=' | sed 's/^/export /')"

# ovh METHOD PATH [JSON_BODY] prints the response body; fails on non-2xx.
ovh() {
  local method="$1" url="$API$2" body="${3:-}" ts sig out code
  ts=$(curl -s "$API/auth/time")
  sig="\$1\$$(printf '%s+%s+%s+%s+%s+%s' "$OVH_APPLICATION_SECRET" "$OVH_CONSUMER_KEY" "$method" "$url" "$body" "$ts" | sha1sum | cut -d' ' -f1)"
  out=$(mktemp)
  code=$(curl -s -o "$out" -w '%{http_code}' -X "$method" \
    -H "X-Ovh-Application: $OVH_APPLICATION_KEY" -H "X-Ovh-Consumer: $OVH_CONSUMER_KEY" \
    -H "X-Ovh-Timestamp: $ts" -H "X-Ovh-Signature: $sig" \
    ${body:+-H "Content-Type: application/json" --data "$body"} "$url")
  cat "$out"; rm -f "$out"
  [[ "$code" == 2* ]] || { echo >&2; echo "OVH $method $2 -> HTTP $code" >&2; return 1; }
}

record_ids() { ovh GET "/domain/zone/$ZONE/record?subDomain=$1" | jq -r '.[]'; }

case "${1:-}" in
  get)
    for id in $(record_ids "$2"); do
      ovh GET "/domain/zone/$ZONE/record/$id" | jq -c '{id, subDomain, fieldType, target, ttl}'
    done
    ;;
  set-a)
    sub="$2" ip="$3" ttl="${4:-300}"
    for id in $(record_ids "$sub"); do
      ovh DELETE "/domain/zone/$ZONE/record/$id" >/dev/null
    done
    ovh POST "/domain/zone/$ZONE/record" \
      "$(jq -cn --arg s "$sub" --arg t "$ip" --argjson ttl "$ttl" '{fieldType:"A",subDomain:$s,target:$t,ttl:$ttl}')" \
      | jq -c '{id, subDomain, fieldType, target, ttl}'
    ovh POST "/domain/zone/$ZONE/refresh" >/dev/null
    echo "refreshed $ZONE"
    ;;
  delete)
    for id in $(record_ids "$2"); do
      ovh DELETE "/domain/zone/$ZONE/record/$id" >/dev/null && echo "deleted $id"
    done
    ovh POST "/domain/zone/$ZONE/refresh" >/dev/null
    ;;
  *)
    sed -n '2,15p' "$0"; exit 2
    ;;
esac
