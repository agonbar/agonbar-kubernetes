#!/usr/bin/env bash
# Configure a fresh BookOrbit (lamg/piracy) through its API: first admin, the two
# libraries, Prowlarr, qBittorrent and the per-medium request destinations.
# Every step skips what already exists, so rerunning it is safe.
#
#   BOOKORBIT_ADMIN_PASSWORD=... scripts/bookorbit-bootstrap.sh
#
# Secrets come from the cluster: the setup token from secret piracy/bookorbit,
# the qBittorrent password from piracy/qbit-creds, the Prowlarr API key from its
# config.xml.
set -euo pipefail

CTX=${CTX:-lamg}
NS=piracy
B=${BOOKORBIT_URL:-https://bookorbit.adriangonzalezbarbosa.eu}/api/v1
ADMIN_USER=${BOOKORBIT_ADMIN_USER:-agonbar}
: "${BOOKORBIT_ADMIN_PASSWORD:?set BOOKORBIT_ADMIN_PASSWORD}"
J='Content-Type: application/json'

k() { kubectl --context "$CTX" -n "$NS" "$@"; }
secret() { k get secret "$1" -o jsonpath="{.data.$2}" | base64 -d; }
api() { # method path [body]
  local out code
  # Fastify rejects a JSON content type with an empty body, so only send it with one.
  out=$(curl -sS -X "$1" "$B$2" -H "$AUTH" ${3:+-H "$J" -d "$3"} -w '\n%{http_code}')
  code=${out##*$'\n'}; out=${out%$'\n'*}
  if [[ $code != 2* ]]; then echo "FAIL $1 $2 -> $code: $out" >&2; exit 1; fi
  printf '%s' "$out"
}
# /libraries returns a bare array; the admin lists wrap it ({managers: [...]}, {clients: [...]}).
find_id() { jq -r --arg n "$2" '(if type=="array" then . else (.managers // .clients) end)[] | select(.name==$n) | .id' <<<"$1" | head -1; }

# 1. Admin
if [[ $(curl -sS "$B/auth/setup-status" | jq -r .needsSetup) == true ]]; then
  echo "creating admin $ADMIN_USER"
  body=$(jq -n --arg u "$ADMIN_USER" --arg p "$BOOKORBIT_ADMIN_PASSWORD" \
    '{username:$u, name:"Adrián", email:"admin@example.invalid", password:$p}')
  TOKEN=$(curl -sS -X POST "$B/auth/setup" -H "$J" -H "x-setup-token: $(secret bookorbit SETUP_BOOTSTRAP_TOKEN)" -d "$body" | jq -r .accessToken)
else
  body=$(jq -n --arg u "$ADMIN_USER" --arg p "$BOOKORBIT_ADMIN_PASSWORD" '{username:$u, password:$p}')
  TOKEN=$(curl -sS -X POST "$B/auth/login" -H "$J" -d "$body" | jq -r .accessToken)
fi
[[ -n $TOKEN && $TOKEN != null ]] || { echo "no access token" >&2; exit 1; }
AUTH="Authorization: Bearer $TOKEN"

# 2. Libraries. Folder as Book cannot be changed after creation; it still treats
# differently named files in one folder as separate books.
libs=$(api GET /libraries)
BOOKS=$(find_id "$libs" Libros)
if [[ -z $BOOKS ]]; then
  BOOKS=$(api POST /libraries '{"name":"Libros","icon":"Library","folders":["/library/books"],
    "organizationMode":"book_per_folder","coverAspectRatio":"2/3",
    "allowedFormats":["epub","kepub","azw3","mobi","azw","fb2","pdf","cbz","cbr","cb7"],
    "formatPriority":["epub","kepub","azw3","mobi","azw","fb2","pdf","cbz","cbr","cb7"],
    "watch":false,"autoScanCronExpression":"0 * * * *"}' | jq -r .id)
  echo "created library Libros ($BOOKS)"
fi
AUDIO=$(find_id "$libs" Audiolibros)
if [[ -z $AUDIO ]]; then
  AUDIO=$(api POST /libraries '{"name":"Audiolibros","icon":"Headphones","folders":["/library/audiobooks"],
    "organizationMode":"book_per_folder","coverAspectRatio":"1/1",
    "allowedFormats":["m4b","m4a","mp3","opus","ogg","flac"],
    "formatPriority":["m4b","m4a","mp3","opus","ogg","flac"],
    "watch":false,"autoScanCronExpression":"0 * * * *"}' | jq -r .id)
  echo "created library Audiolibros ($AUDIO)"
fi

# 3. Prowlarr. Creating it already syncs the indexers; seed goals are inherited
# from each Prowlarr indexer and can only be changed there.
PM=$(find_id "$(api GET /admin/request-indexer-managers)" Prowlarr)
if [[ -z $PM ]]; then
  key=$(k exec deploy/prowlarr-deployment -- sh -c 'sed -n "s:.*<ApiKey>\(.*\)</ApiKey>.*:\1:p" /config/config.xml')
  body=$(jq -n --arg k "$key" '{name:"Prowlarr", type:"prowlarr",
    baseUrl:"http://prowlarr.piracy.svc.cluster.local:9696", credential:$k,
    allowPrivateAddress:true, syncNewIndexers:true, inheritSeedLimits:true}')
  PM=$(api POST /admin/request-indexer-managers "$body" | jq -r .id)
  echo "created Prowlarr manager ($PM)"
fi
echo "prowlarr test: $(api POST "/admin/request-indexer-managers/$PM/test" | jq -c .)"

# 4. qBittorrent. It sees the torrents dataset at /downloads, same as this pod,
# and the Book Dock lives there too so the import hardlink works on NFS.
DC=$(find_id "$(api GET /admin/download-clients)" qBittorrent)
if [[ -z $DC ]]; then
  body=$(jq -n --arg p "$(secret qbit-creds password)" '{name:"qBittorrent", adapterType:"qbittorrent",
    baseUrl:"http://qbittorrent.piracy.svc.cluster.local:8080", username:"admin", password:$p,
    category:"bookorbit", useHardlinks:true, allowPrivateAddress:true, priority:1,
    pathMappings:[{remotePath:"/downloads", localPath:"/downloads"}]}')
  DC=$(api POST /admin/download-clients "$body" | jq -r .id)
  echo "created qBittorrent client ($DC)"
fi
echo "qbittorrent test: $(api POST "/admin/download-clients/$DC/test" | jq -c .)"
MAP=$(api GET "/admin/download-clients/$DC" | jq -r '.pathMappings[0].id')
echo "hardlink test: $(api POST "/admin/download-clients/$DC/test-path-mapping" "{\"mappingId\":$MAP}" | jq -c .)"

# 5. Where fulfilled requests land.
body=$(jq -n --argjson b "$BOOKS" --argjson a "$AUDIO" \
  '{destinations:{ebook:{libraryId:$b}, comic:{libraryId:$b}, audiobook:{libraryId:$a}}}')
api PUT /admin/book-request-automation "$body" >/dev/null
echo "request destinations: $(api GET /admin/book-request-automation | jq -c .destinations)"
