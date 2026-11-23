#!/usr/bin/env bash
# List the images running in a cluster that have a newer tag of the same shape
# in their registry. Read-only: it never touches the cluster or git.
#
#   scripts/image-updates.sh [context] [namespace-regex]
#
# "Same shape" means every digit run in the current tag may change and nothing
# else may: 1.2.3 only compares against N.N.N, v2.5.1-alpine against
# vN.N.N-alpine. Tags without digits (latest, stable) and digest-only images are
# reported as unpinned and skipped. Needs regctl (nix shell nixpkgs#regclient).
set -euo pipefail

ctx=${1:-lamg}
ns_re=${2:-.}

if ! command -v regctl >/dev/null; then
  exec nix shell nixpkgs#regclient -c "$0" "$@"
fi

images=$(kubectl --context "$ctx" get pods -A -o json | jq -r --arg re "$ns_re" '
  .items[] | select(.metadata.namespace | test($re)) | .metadata.namespace as $ns
  | (.spec.containers + (.spec.initContainers // []))[] | "\($ns) \(.image)"' |
  sort -u -k2,2)

unpinned=()
while read -r ns image; do
  ref=${image%@*}
  if [[ ${ref##*/} != *:* ]]; then
    unpinned+=("$ns $image"); continue
  fi
  repo=${ref%:*}; tag=${ref##*:}
  if [[ ! $tag =~ [0-9] ]]; then unpinned+=("$ns $image"); continue; fi

  # Build a regex from the tag: escape dots, turn each digit run into [0-9]+.
  pattern="^$(sed -E 's/[.]/\\./g; s/[0-9]+/[0-9]+/g' <<<"$tag")\$"
  tags=$(regctl tag ls "$repo" 2>/dev/null) || { echo "?? $ns $image (cannot list tags)"; continue; }
  newest=$( (grep -E "$pattern" <<<"$tags"; echo "$tag") | sort -V | tail -1)
  if [[ $newest != "$tag" ]]; then
    printf '%-14s %-60s %s -> %s\n' "$ns" "$repo" "$tag" "$newest"
  fi
done <<<"$images"

if ((${#unpinned[@]})); then
  echo
  echo "Unpinned (latest/digest/no digits), not checked:"
  printf '  %s\n' "${unpinned[@]}"
fi
