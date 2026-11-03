#!/usr/bin/env bash
# Repairs a season pack that qBittorrent marked complete while the NFS quota
# under it was full: the failed writes never surfaced, so the files are
# partly zeros and Sonarr imported (and kept) the broken episodes.
#
# Run it after a forced recheck. It waits for the recheck, resumes the torrent,
# waits for 100 %, checks every file is fully allocated, deletes the broken
# episode files in Sonarr so it re-imports them from the pack, waits for the
# import, re-checks the library, then deletes the torrent and reverts the
# temporary refquota and cruncharr pause. Any failed check stops it without
# deleting anything.
#
#   HASH=<torrent hash> SERIES_ID=<sonarr series id> scripts/qbit-pack-repair.sh
#
# Optional: SUDO_PASS_FILE (nas02 sudo password, deleted after use) to revert
# the refquota saved in the agonbar:refquota-orig property of SSD/torrents.
set -euo pipefail

HASH="${HASH:?}"; SERIES_ID="${SERIES_ID:?}"
CTX="${CTX:-lamg}"; NS="${NS:-piracy}"
REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
K="kubectl --context=$CTX -n $NS"
log() { echo "$(date '+%F %T') $*"; }
die() { log "STOP: $*"; exit 1; }

QBIT_PW=$($K get secret qbit-creds -o jsonpath='{.data.password}' | base64 -d)
# Every call into a pod is retried: a node that goes NotReady for a few
# minutes (orange-pi5 under load) makes `kubectl exec` fail with a 502.
retry() { # retry <cmd...> ; stdin is buffered so it can be replayed
  local in out i; in=$(cat)
  for i in $(seq 1 15); do
    out=$("$@" <<<"$in") && { printf '%s\n' "$out"; return 0; }
    log "retry $i: $1 failed" >&2; sleep 60
  done
  return 1
}
qbit() { retry qbit_once "$@" </dev/null; }
qbit_once() {
  $K exec -i deploy/qbittorrent-deployment -c qbittorrent -- sh -c "
    curl -s -c /tmp/qc -X POST 'http://localhost:8080/api/v2/auth/login' \
      -H 'Referer: http://localhost:8080' \
      --data-urlencode 'username=admin' --data-urlencode \"password=\$1\" >/dev/null
    shift; curl -s -b /tmp/qc \"\$@\"" _ "$QBIT_PW" "$@"
}
torrent() { qbit "http://localhost:8080/api/v2/torrents/info?hashes=$HASH" | jq -c '.[0]'; }

SONARR_KEY=$($K exec deploy/sonarr-deployment -c sonarr -- \
  sh -c 'grep -o "<ApiKey>[^<]*" /config/config.xml | cut -c9-')
sonarr() { retry sonarr_once "$@"; } # sonarr <method> <path> [json body on stdin]
sonarr_once() {
  $K exec -i deploy/sonarr-deployment -c sonarr -- curl -sf -m 300 -X "$1" \
    -H "X-Api-Key: $SONARR_KEY" -H 'Content-Type: application/json' \
    --data-binary @- "http://localhost:8989/api/v3/$2"
}

# alloc_report <deploy> <container> : reads paths on stdin, prints "<pct> <path>"
# (allocated / apparent size; ZFS stores zero runs as nothing, so <95 % = holes)
alloc_report() { retry alloc_report_once "$@"; }
alloc_report_once() {
  $K exec -i "deploy/$1" -c "$2" -- sh -c '
    while IFS= read -r f; do
      s=$(stat -c %s "$f"); k=$(du -k "$f" | cut -f1)
      [ "$s" -gt 0 ] && echo "$((k * 1024 * 100 / s)) $f"
    done'
}

# --- 1. recheck done, resume, wait for 100 % --------------------------------
log "waiting for recheck"
while [[ $(torrent | jq -r .state) == checking* ]]; do sleep 60; done
log "after recheck: $(torrent | jq -r '"\(.state) \(.progress*1000|floor/10)% left=\(.amount_left/1073741824|floor)G"')"

qbit -X POST http://localhost:8080/api/v2/torrents/setDownloadLimit \
  --data "hashes=$HASH" --data "limit=$(( ${DL_LIMIT_MIB:-4} * 1048576 ))" >/dev/null
qbit -X POST http://localhost:8080/api/v2/torrents/start --data "hashes=$HASH" >/dev/null
qbit -X POST http://localhost:8080/api/v2/torrents/resume --data "hashes=$HASH" >/dev/null
log "resumed"

deadline=$(( $(date +%s) + 48 * 3600 ))
while :; do
  t=$(torrent)
  [ "$(jq -r .progress <<<"$t")" = 1 ] && [[ $(jq -r .state <<<"$t") != checking* ]] && break
  [[ $(jq -r .state <<<"$t") =~ ^(error|missingFiles)$ ]] && die "torrent state $(jq -r .state <<<"$t")"
  [ "$(date +%s)" -lt "$deadline" ] || die "not complete after 48 h"
  log "$(jq -r '"\(.state) \(.progress*1000|floor/10)% left=\(.amount_left/1073741824|floor)G"' <<<"$t")"
  sleep 600
done
log "download complete"

# --- 2. the pack is really on disk -----------------------------------------
CONTENT=$(torrent | jq -r .content_path)
bad=$(qbit "http://localhost:8080/api/v2/torrents/files?hash=$HASH" |
  jq -r --arg c "$(dirname "$CONTENT")" '.[] | "\($c)/\(.name)"' |
  alloc_report qbittorrent-deployment qbittorrent | awk '$1 < 95' | wc -l)
[ "$bad" -eq 0 ] || die "$bad files in the pack still have holes"
log "pack verified: every file fully allocated"

# --- 3. delete the broken episode files so Sonarr re-imports them ----------
sonarr GET "episodefile?seriesId=$SERIES_ID" </dev/null |
  jq -r '.[] | "\(.id)\t\(.path)"' > /tmp/pack-repair-files.tsv
cut -f2 /tmp/pack-repair-files.tsv | alloc_report sonarr-deployment sonarr |
  awk '$1 < 95 {sub(/^[0-9]+ /, ""); print}' > /tmp/pack-repair-broken.txt
IDS=$(grep -F -f /tmp/pack-repair-broken.txt /tmp/pack-repair-files.tsv | cut -f1 | jq -Rs 'split("\n") | map(select(length>0) | tonumber)')
log "broken library files: $(jq length <<<"$IDS")"
# the episodes behind those files, to know when they have a file again
EPS=$(sonarr GET "episode?seriesId=$SERIES_ID" </dev/null |
  jq -c --argjson ids "$IDS" '[.[] | select(.episodeFileId as $f | $ids | index($f)) | .id]')
if [ "$(jq length <<<"$IDS")" -gt 0 ]; then
  jq -n --argjson ids "$IDS" '{episodeFileIds: $ids}' | sonarr DELETE episodefile/bulk >/dev/null
fi
log "deleted them ($(jq length <<<"$EPS") episodes); waiting for Sonarr to re-import from the pack"

# --- 4. wait for those episodes to have a file again, then re-check --------
# (not "until the pack leaves the queue": episodes that are no upgrade keep
# it in importPending forever)
deadline=$(( $(date +%s) + 6 * 3600 ))
while :; do
  echo '{"name":"RefreshMonitoredDownloads"}' | sonarr POST command >/dev/null
  sleep 300
  left=$(sonarr GET "episode?seriesId=$SERIES_ID" </dev/null |
    jq --argjson e "$EPS" '[.[] | select(.id as $i | $e | index($i)) | select(.hasFile | not)] | length')
  log "episodes still without file: $left"
  [ "$left" -eq 0 ] && break
  [ "$(date +%s)" -lt "$deadline" ] || die "Sonarr did not re-import them in 6 h, torrent kept for a manual import"
done
sonarr GET "episodefile?seriesId=$SERIES_ID" </dev/null | jq -r '.[].path' |
  alloc_report sonarr-deployment sonarr > /tmp/pack-repair-after.txt
still=$(awk '$1 < 95' /tmp/pack-repair-after.txt | wc -l)
log "library: $(wc -l < /tmp/pack-repair-after.txt) files, $still with holes"
[ "$still" -eq 0 ] || die "library still has broken files, torrent kept for a manual import"

# --- 5. clean up and revert the temporary changes --------------------------
qbit -X POST http://localhost:8080/api/v2/torrents/delete \
  --data "hashes=$HASH" --data deleteFiles=true >/dev/null
log "torrent deleted with its files"

if [ -n "${SUDO_PASS_FILE:-}" ] && [ -r "$SUDO_PASS_FILE" ]; then
  sleep 30 # let ZFS account the freed space before lowering the quota
  ssh -o BatchMode=yes nas02 'export PATH=$PATH:/usr/sbin:/sbin; read -r P
    s() { printf "%s\n" "$P" | sudo -S -p "" "$@"; }
    orig=$(zfs get -H -o value agonbar:refquota-orig SSD/torrents)
    [ "$orig" != "-" ] && s zfs set refquota="$orig" SSD/torrents && s zfs inherit agonbar:refquota-orig SSD/torrents
    zfs get -H -o property,value refquota,available SSD/torrents' < "$SUDO_PASS_FILE"
  rm -f "$SUDO_PASS_FILE"
  log "refquota reverted"
fi

WT=$(mktemp -d)
git -C "$REPO" fetch -q origin
git -C "$REPO" worktree add -q --detach "$WT" origin/main
python3 - "$WT/deployments/piracy/cruncharr.yml" <<'EOF'
import re, sys
p = sys.argv[1]; s = open(p).read()
s2 = re.sub(r'            # TEMPORARY \(2026-11-03\).*?value: "80000"\n', '', s, flags=re.S)
assert s2 != s, "temporary cruncharr pause not found"
open(p, 'w').write(s2)
EOF
git -C "$WT" commit -qam "cruncharr: resume downloads, the InuYasha pack is repaired

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git -C "$WT" push -q origin HEAD:main
git -C "$REPO" worktree remove --force "$WT"
kubectl --context="$CTX" -n argocd annotate applications.argoproj.io piracy argocd.argoproj.io/refresh=normal --overwrite >/dev/null
log "cruncharr pause reverted"
log "DONE"
