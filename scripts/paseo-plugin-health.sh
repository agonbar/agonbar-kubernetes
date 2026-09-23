#!/usr/bin/env bash
# Report the real state of Paseo plugins before installing them.
#
# The paseo.cafe catalog caches repo metadata, so a plugin can show as healthy
# there while its GitHub repo is archived or its npm package is stale. This
# cross-checks the catalog entry against GitHub and npm.
#
# Usage: paseo-plugin-health.sh [plugin-id ...]
#        paseo-plugin-health.sh --all
set -euo pipefail

CATALOG_URL=${CATALOG_URL:-https://paseo.cafe/api/plugins}
cache=$(mktemp)
trap 'rm -f "$cache"' EXIT

curl -sSf -m 60 "$CATALOG_URL" -o "$cache"

if [[ ${1:-} == "--all" ]]; then
  mapfile -t ids < <(jq -r '.plugins[].id' "$cache")
elif [[ $# -gt 0 ]]; then
  ids=("$@")
else
  echo "usage: $(basename "$0") [plugin-id ...] | --all" >&2
  exit 2
fi

printf '%-22s %-32s %-9s %-11s %-6s %-11s %-9s %s\n' \
  PLUGIN REPO ARCHIVED PUSHED STARS NPM DL30 REQUIRES

for id in "${ids[@]}"; do
  entry=$(jq -c --arg id "$id" '.plugins[] | select(.id == $id)' "$cache")
  if [[ -z $entry ]]; then
    printf '%-22s %s\n' "$id" "NOT IN CATALOG"
    continue
  fi

  repo=$(jq -r '.repo' <<<"$entry")
  pkg=$(jq -r '.npm.package // .package // empty' <<<"$entry")
  req=$(jq -r '.paseoVersionRequirement // "-"' <<<"$entry")

  if gh_json=$(gh api "repos/$repo" 2>/dev/null); then
    archived=$(jq -r 'if .archived then "ARCHIVED" else "no" end' <<<"$gh_json")
    pushed=$(jq -r '.pushed_at[0:10]' <<<"$gh_json")
    stars=$(jq -r '.stargazers_count' <<<"$gh_json")
    note=$(jq -r 'if .archived then "read-only upstream" elif .disabled then "disabled" else "" end' <<<"$gh_json")
  else
    archived=GONE; pushed=-; stars=-; note="repo unreachable"
  fi

  if [[ -n $pkg ]]; then
    npm=$(jq -r '.npm.publishedAt[0:10]' <<<"$entry")
    dl30=$(jq -r '.npm.downloadsLast30Days // "-"' <<<"$entry")
    curl -sf -m 20 -o /dev/null "https://registry.npmjs.org/${pkg}" || note="npm package missing"
  else
    npm="(repo)"; dl30="-"
  fi

  printf '%-22s %-32s %-9s %-11s %-6s %-11s %-9s %s\n' \
    "$id" "$repo" "$archived" "$pushed" "$stars" "$npm" "$dl30" "$req${note:+  ($note)}"
done
