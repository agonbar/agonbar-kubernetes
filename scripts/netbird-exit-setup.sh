#!/usr/bin/env bash
# NetBird management side of the two internet exits (deployments/netbird-exit).
# Idempotent: creates whatever is missing, never edits or deletes.
#
#   exit-users   SSO users allowed to use the exits (set as their user auto_group)
#   exit-home    the exit pod on a lamg house node
#   exit-ovh     the exit pod on ovh02
#   route        "Salida casa" / "Salida OVH": 0.0.0.0/0 via each pod, masquerade,
#                distributed to exit-users only
#   policy       exit-users -> exit-home / exit-ovh, nothing else
#
# The setup keys go straight into the Secret netbird-exit/netbird-exit-keys
# (keys NB_SETUP_KEY_HOME / NB_SETUP_KEY_OVH), never to stdout or git.
# The exits are fenced off from anything private by the NetworkPolicy in the
# manifest, not by NetBird: an exit route covers 10/8 and 192.168/16 too.
#
#   scripts/netbird-exit-setup.sh            create what is missing
#   scripts/netbird-exit-setup.sh status     show what exists
set -euo pipefail

API=https://netbird.senseivision.ai/api
CTX=${KUBE_CONTEXT:-lamg}
TOKEN=$(bitwarden-mcp bw get password netbird-pat 2>/dev/null)
[ -n "$TOKEN" ] || { echo "no netbird-pat in Bitwarden" >&2; exit 1; }

nb() { # nb METHOD PATH [JSON]
  curl -sf -X "$1" -H "Authorization: Token $TOKEN" -H 'Content-Type: application/json' \
    "$API/$2" ${3:+--data "$3"}
}

group_id() { nb GET groups | jq -r --arg n "$1" '.[] | select(.name == $n) | .id'; }

ensure_group() {
  local id; id=$(group_id "$1")
  if [ -z "$id" ]; then
    id=$(nb POST groups "$(jq -nc --arg n "$1" '{name: $n, peers: []}')" | jq -r .id)
    echo "group $1 created" >&2
  fi
  echo "$id"
}

ensure_setup_key() { # name group_id secret_key
  if nb GET setup-keys | jq -e --arg n "$1" '.[] | select(.name == $n and .valid)' >/dev/null; then
    return
  fi
  local key
  key=$(nb POST setup-keys "$(jq -nc --arg n "$1" --arg g "$2" \
    '{name: $n, type: "reusable", expires_in: 31536000, auto_groups: [$g], usage_limit: 0, ephemeral: true}')" | jq -r .key)
  kubectl --context "$CTX" create namespace netbird-exit --dry-run=client -o yaml | kubectl --context "$CTX" apply -f - >/dev/null
  kubectl --context "$CTX" -n netbird-exit patch secret netbird-exit-keys --type merge \
    -p "$(jq -nc --arg k "$3" --arg v "$key" '{stringData: {($k): $v}}')" >/dev/null 2>&1 ||
    kubectl --context "$CTX" -n netbird-exit create secret generic netbird-exit-keys --from-literal="$3=$key" >/dev/null
  echo "setup key $1 created, stored as $3" >&2
}

ensure_route() { # network_id description peer_group users_group skip_auto_apply
  if nb GET routes | jq -e --arg n "$1" '.[] | select(.network_id == $n)' >/dev/null; then return; fi
  nb POST routes "$(jq -nc --arg n "$1" --arg d "$2" --arg pg "$3" --arg ug "$4" --argjson skip "$5" \
    '{network_id: $n, description: $d, enabled: true, network: "0.0.0.0/0", metric: 9999,
      masquerade: true, keep_route: true, peer_groups: [$pg], groups: [$ug],
      access_control_groups: [$pg], skip_auto_apply: $skip}')" >/dev/null
  echo "route $1 created" >&2
}

ensure_policy() { # name src dst
  if nb GET policies | jq -e --arg n "$1" '.[] | select(.name == $n)' >/dev/null; then return; fi
  nb POST policies "$(jq -nc --arg n "$1" --arg s "$2" --arg d "$3" \
    '{name: $n, enabled: true, rules: [{name: $n, enabled: true, action: "accept",
      bidirectional: false, protocol: "all", sources: [$s], destinations: [$d]}]}')" >/dev/null
  echo "policy $1 created" >&2
}

if [ "${1:-}" = status ]; then
  nb GET groups | jq -r '.[] | select(.name | startswith("exit-")) | "group  \(.name)  peers=\(.peers_count)"'
  nb GET routes | jq -r '.[] | select(.network_id | startswith("exit-")) | "route  \(.network_id)  \(.network)  enabled=\(.enabled)"'
  nb GET policies | jq -r '.[] | select(.name | startswith("exit-")) | "policy \(.name)"'
  nb GET peers | jq -r '.[] | select(any(.groups[]; .name | startswith("exit-"))) |
    "peer   \(.name)  \(.ip)  connected=\(.connected)  groups=\(.groups | map(.name) | join(","))"'
  exit
fi

users=$(ensure_group exit-users)
home=$(ensure_group exit-home)
ovh=$(ensure_group exit-ovh)
ensure_setup_key netbird-exit-home "$home" NB_SETUP_KEY_HOME
ensure_setup_key netbird-exit-ovh "$ovh" NB_SETUP_KEY_OVH
# Home is the default exit; OVH is opt-in from the client menu.
ensure_route exit-home "Salida casa" "$home" "$users" false
ensure_route exit-ovh "Salida OVH" "$ovh" "$users" true
ensure_policy exit-users-to-home "$users" "$home"
ensure_policy exit-users-to-ovh "$users" "$ovh"
