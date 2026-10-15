#!/usr/bin/env bash
# Syncthing on nas03, as an inbox for someone who has to send us several TB.
#
# nas03 had no Syncthing of its own: the lamg pod runs on orange-pi5 and stores on nas02,
# which has no room for this. This installs the TrueNAS `syncthing` app with host
# networking, so QUIC NAT traversal and LAN discovery work from behind the aya router, and
# creates the folders as Receive Only, so nothing done on nas03 travels back to the sender.
#
#   ./nas03-syncthing-inbox.sh setup            # dataset, app, GUI login, folders
#   ./nas03-syncthing-inbox.sh info             # what the sender needs: device ID, folder IDs
#   ./nas03-syncthing-inbox.sh pending          # devices that knocked and are not accepted yet
#   ./nas03-syncthing-inbox.sh peer ID [NAME]   # accept the sender, share the folders with them
#   ./nas03-syncthing-inbox.sh status           # listeners, connections, per-folder progress
#
# setup is idempotent. No sudo anywhere: the API key is passed to the app as STGUIAPIKEY, so
# it can be read back with `midclt call app.config` instead of from the root-only config.xml.
#
# A Receive Only folder still applies the sender's deletions. Before they wipe their copy,
# take them off the folder (GUI -> folder -> Sharing), or the data goes from nas03 too.
set -euo pipefail

DS=RAID/ivan
MNT=/ivan               # where $DS is mounted inside the container
FOLDERS=(ivan-1 ivan-2) # folder IDs, and directory names under $DS
PORT=20910              # GUI/API on the host; sync itself listens on Syncthing's default 22000
OWNER=950               # truenas_admin, so the files can be moved over SSH without sudo
GUI_USER=agonbar
N03=(ssh -o BatchMode=yes -o ConnectTimeout=10 "${N03_HOST:-nas03}")

log(){ echo "[$(date +%H:%M:%S)] $*"; }
die(){ echo "FATAL: $*" >&2; exit 1; }
rand(){ LC_ALL=C tr -dc 'a-zA-Z0-9' </dev/urandom | head -c "$1" || true; }
apikey(){ "${N03[@]}" "midclt call app.config syncthing" |
  jq -r '.syncthing.additional_envs[] | select(.name=="STGUIAPIKEY").value'; }
# st METHOD PATH [JSON]: the body goes over stdin, so it never meets the remote shell's quoting.
st(){ printf '%s' "${3:-}" | "${N03[@]}" "curl -sf -X $1 -H 'X-API-Key: $KEY' \
  ${3:+-H 'Content-Type: application/json' -d @-} 'http://127.0.0.1:$PORT$2'"; }
myid(){ st GET /rest/system/status | jq -r .myID; }

setup() {
  if "${N03[@]}" "midclt call pool.dataset.query '[[\"id\",\"=\",\"$DS\"]]'" | grep -q '"id"'; then
    log "nas03: $DS already exists"
  else
    "${N03[@]}" "midclt call pool.dataset.create '{\"name\": \"$DS\", \"share_type\": \"GENERIC\",
      \"acltype\": \"POSIX\", \"aclmode\": \"DISCARD\", \"compression\": \"LZ4\", \"recordsize\": \"1M\", \"atime\": \"OFF\"}'" >/dev/null
    "${N03[@]}" "midclt call -j filesystem.setperm '{\"path\": \"/mnt/$DS\", \"uid\": $OWNER, \"gid\": $OWNER, \"mode\": \"770\"}'" >/dev/null
    log "nas03: created $DS, owned by uid $OWNER"
  fi

  if "${N03[@]}" "midclt call app.query '[[\"name\",\"=\",\"syncthing\"]]'" | grep -q '"name"'; then
    log "nas03: app syncthing already installed"
  else
    ver=$("${N03[@]}" "midclt call app.available" |
      jq -r '.[] | select(.name=="syncthing" and .train=="stable") | .latest_version')
    log "nas03: installing app syncthing $ver"
    jq -nc --arg v "$ver" --arg k "$(rand 32)" --arg mnt "$MNT" --arg ds "/mnt/$DS" \
      --argjson port "$PORT" --argjson o "$OWNER" '{
      app_name: "syncthing", catalog_app: "syncthing", train: "stable", version: $v,
      values: {
        TZ: "Europe/Madrid",
        syncthing: {additional_envs: [{name: "STGUIAPIKEY", value: $k}]},
        run_as: {user: $o, group: $o},
        network: {host_network: true, web_port: {bind_mode: "published", port_number: $port}},
        resources: {limits: {cpus: 2, memory: 4096}},
        storage: {
          config: {type: "ix_volume", ix_volume_config: {acl_enable: false, dataset_name: "config"}},
          additional_storage: [{type: "host_path", read_only: false, mount_path: $mnt,
            host_path_config: {acl_enable: false, path: $ds}}]
        }
      }}' | "${N03[@]}" 'f=$(mktemp) && cat > $f && midclt call -j app.create "$(cat $f)" >/dev/null; rc=$?; rm -f $f; exit $rc'
  fi
  # app.create leaves the app STOPPED -- it does not start it for you.
  "${N03[@]}" "midclt call app.query '[[\"name\",\"=\",\"syncthing\"]]'" | jq -e '.[0].state=="RUNNING"' >/dev/null ||
    "${N03[@]}" "midclt call -j app.start syncthing" >/dev/null

  KEY=$(apikey)
  for _ in $(seq 1 30); do st GET /rest/system/ping >/dev/null 2>&1 && break; sleep 5; done
  st GET /rest/system/ping >/dev/null || die "the Syncthing API on nas03:$PORT does not answer"

  if [ -z "$(st GET /rest/config/gui | jq -r .user)" ]; then
    pw=$(rand 24)
    st PATCH /rest/config/gui "$(jq -nc --arg u "$GUI_USER" --arg p "$pw" '{user: $u, password: $p}')"
    log "nas03: GUI login set, shown this once: $GUI_USER / $pw"
  fi
  st PATCH "/rest/config/devices/$(myid)" '{"name": "nas03"}'

  for f in "${FOLDERS[@]}"; do
    if st GET "/rest/config/folders/$f" >/dev/null 2>&1; then
      log "nas03: folder $f already exists"
    else
      st POST /rest/config/folders "$(jq -nc --arg f "$f" --arg p "$MNT/$f" \
        '{id: $f, label: $f, path: $p, type: "receiveonly"}')"
      log "nas03: folder $f -> /mnt/$DS/$f (receive only)"
    fi
  done
  info
}

info() {
  KEY=${KEY:-$(apikey)}
  echo "Device ID: $(myid)"
  echo "Folder IDs: ${FOLDERS[*]}"
}

pending() {
  KEY=$(apikey)
  st GET /rest/cluster/pending/devices | jq -r 'to_entries[] | "\(.key)  \(.value.name)  \(.value.address)  \(.value.time)"'
}

peer() {
  local id=${1:?usage: peer DEVICE_ID [NAME]} name=${2:-ivan}
  KEY=$(apikey)
  st GET "/rest/config/devices/$id" >/dev/null 2>&1 ||
    st POST /rest/config/devices "$(jq -nc --arg i "$id" --arg n "$name" '{deviceID: $i, name: $n}')"
  for f in "${FOLDERS[@]}"; do
    st PUT "/rest/config/folders/$f" "$(st GET "/rest/config/folders/$f" |
      jq -c --arg i "$id" 'if any(.devices[]; .deviceID==$i) then . else .devices += [{deviceID: $i}] end')"
  done
  log "nas03: $name ($id) accepted on ${FOLDERS[*]}"
}

status() {
  KEY=$(apikey)
  echo "listeners:"
  st GET /rest/system/status | jq -r '.connectionServiceStatus | to_entries[]
    | "  \(.key)  lan=\(.value.lanAddresses // [] | join(","))  wan=\(.value.wanAddresses // [] | join(","))  \(.value.error // "")"'
  echo "connections:"
  st GET /rest/system/connections | jq -r '.connections | to_entries[] | select(.value.connected)
    | "  \(.key[0:7])  \(.value.type)  \(.value.address)  in=\(.value.inBytesTotal / 1e9 * 100 | round / 100)GB"'
  for f in "${FOLDERS[@]}"; do
    st GET "/rest/db/status?folder=$f" | jq -r --arg f "$f" \
      '"\($f): \(.state)  have=\(.localBytes / 1e9 * 100 | round / 100)GB  need=\(.needBytes / 1e9 * 100 | round / 100)GB  of \(.globalBytes / 1e9 * 100 | round / 100)GB"'
  done
}

case "${1:-}" in
  setup|info|pending|status) "$1" ;;
  peer) shift; peer "$@" ;;
  *) sed -n '2,19p' "$0"; exit 1 ;;
esac
