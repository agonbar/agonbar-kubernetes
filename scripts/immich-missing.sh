#!/usr/bin/env bash
# List photos/videos under a local directory that are NOT already in Immich.
#
# Immich stores the SHA-1 of every original in asset.checksum, so this hashes the
# local files and diffs them against the DB (read-only, via the postgre pod).
# Matches across all Immich users count as "already there".
#
# Usage: scripts/immich-missing.sh <dir> [--context lamg]
set -euo pipefail

dir=${1:?usage: $0 <dir> [--context ctx]}
ctx=lamg
[[ ${2:-} == --context ]] && ctx=${3:?}

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

kubectl --context "$ctx" -n immich exec deploy/postgre -- sh -c \
  'psql -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-immich}" -Atc "select encode(checksum, '\''hex'\'') from asset"' \
  | sort -u > "$tmp/immich"

find "$dir" -mindepth 1 -name '.*' -prune -o -type f \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.heic' \
  -o -iname '*.webp' -o -iname '*.gif' -o -iname '*.mp4' -o -iname '*.mov' -o -iname '*.3gp' \
  -o -iname '*.dng' \) -print0 \
  | xargs -0 -r sha1sum | sort > "$tmp/local"

total=$(wc -l < "$tmp/local")
join -v1 -o 1.2 <(sed 's/  /\t/' "$tmp/local" | sort -t$'\t' -k1,1) \
  <(sort "$tmp/immich") -t$'\t' > "$tmp/missing" || true
missing=$(wc -l < "$tmp/missing")

cat "$tmp/missing"
echo "# $missing of $total media files under $dir are not in Immich ($(wc -l < "$tmp/immich") assets checked)" >&2
