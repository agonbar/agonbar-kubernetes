#!/usr/bin/env bash
# Join a TrueNAS SCALE box to the senseizero tailnet, matching how nas02 already does it.
#
# The tailnet's control plane is headscale (GKE, ns headscale), not Tailscale SaaS, so the
# client needs --login-server and a headscale pre-auth key. TrueNAS has no tailscaled
# package: it runs the community `tailscale` app as a host-network container, which is why
# `command -v tailscale` returns nothing on a box that is perfectly well connected. Check
# `ip -br addr | grep tailscale0` instead.
#
#   HS_API_KEY=$(bw get item headplane-prd | jq -r '.fields[]|select(.name=="api_key").value') \
#     ./truenas-join-tailnet.sh nas03
#
# The node joins as an untagged device of headscale user `agonbar` (id 1), same as
# nas00/nas01/nas02 -- that is what puts it under the `autogroup:member -> autogroup:self:*`
# ACL rule and nothing else. Do NOT pass a tag-bound key here: tags flip ownership to the
# synthetic `tagged-devices` user and the box drops out of that rule.
set -euo pipefail

HOST=${1:-nas03}
TSNAME=${2:-$HOST}
HS=${HS_URL:-https://headscale.prd.senseizero.es}
HS_USER=${HS_USER_ID:-1}
: "${HS_API_KEY:?falta HS_API_KEY (Bitwarden: item headplane-prd, campo api_key)}"

log(){ echo "[$(date +%H:%M:%S)] $*"; }
api(){ curl -sf -H "Authorization: Bearer $HS_API_KEY" -H 'Content-Type: application/json' "$@"; }

if ssh -o BatchMode=yes "$HOST" "ip -br addr" 2>/dev/null | grep -q tailscale0; then
  log "$HOST ya tiene tailscale0, no hago nada"; exit 0
fi

# Non-reusable and short-lived: it is consumed by this one registration and nothing else.
log "pidiendo pre-auth key a headscale para el usuario $HS_USER"
KEY=$(api -X POST "$HS/api/v1/preauthkey" -d "$(jq -nc --arg u "$HS_USER" \
  --arg e "$(date -u -d '+1 hour' +%Y-%m-%dT%H:%M:%SZ)" \
  '{user:$u, reusable:false, ephemeral:false, expiration:$e}')" | jq -r '.preAuthKey.key')
[ -n "$KEY" ] && [ "$KEY" != null ] || { echo "FATAL: headscale no devolvio key" >&2; exit 1; }

VER=$(ssh -o BatchMode=yes "$HOST" "midclt call app.available" |
  jq -r '.[] | select(.name=="tailscale" and .train=="community") | .latest_version')
log "instalando la app tailscale $VER en $HOST como '$TSNAME'"

# Mirrors nas02's values, with one deliberate difference: advertise_exit_node stays false.
# nas02 already offers the exit node for the house; a second one on a NAS buys nothing and
# would still need approving in headscale anyway.
jq -nc --arg n "$TSNAME" --arg k "$KEY" --arg v "$VER" --arg hs "$HS" '{
  app_name: "tailscale", catalog_app: "tailscale", train: "community", version: $v,
  values: {
    TZ: "Europe/Madrid",
    network: {host_network: true},
    resources: {limits: {cpus: 4, memory: 8192}},
    storage: {state: {type: "ix_volume", ix_volume_config: {acl_enable: false, dataset_name: "state"}}},
    tailscale: {
      accept_dns: true, accept_routes: false, additional_envs: [],
      advertise_exit_node: false, advertise_routes: [],
      auth_key: $k, auth_once: false,
      extra_args: ["--login-server=" + $hs],
      hostname: $n, reset: false, tailscaled_args: [], userspace: false
    }
  }}' | ssh -o BatchMode=yes "$HOST" "cat > /tmp/ts-app.json && midclt call app.create \"\$(cat /tmp/ts-app.json)\"; rm -f /tmp/ts-app.json"

# app.create returns a job id and leaves the app STOPPED -- it does not start it for you.
log "arrancando la app"
ssh -o BatchMode=yes "$HOST" "midclt call app.start tailscale" >/dev/null

log "esperando a que levante"
for _ in $(seq 1 30); do
  ip=$(ssh -o BatchMode=yes "$HOST" "ip -br addr show tailscale0 2>/dev/null" | awk '{print $3}')
  [ -n "$ip" ] && { log "tailscale0 = $ip"; break; }
  sleep 10
done
[ -n "${ip:-}" ] || log "AVISO: tailscale0 sigue sin aparecer; mira 'midclt call app.query' en $HOST"

log "en headscale:"
api "$HS/api/v1/node" | jq -r --arg n "$TSNAME" '.nodes[] | select(.name==$n)
  | "  \(.name) id=\(.id) user=\(.user.name) online=\(.online) ip=\(.ipAddresses[0])"'
