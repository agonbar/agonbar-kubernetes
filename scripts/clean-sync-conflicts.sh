#!/usr/bin/env bash
# Delete Syncthing conflict copies that carry no unique content.
#
# This repo lives in a Syncthing-managed directory, so editing a manifest on
# two machines leaves radarr.sync-conflict-20260903-104530-EZFUC2T.yml beside
# radarr.yml. Almost all of them are stale snapshots of a state already in git,
# but "almost" is why this checks instead of running `rm`: a conflict copy of
# an untracked file (secrets.yaml) or of an edit that was never committed is
# the only copy of that content.
#
# A file is deleted only if its exact bytes appear in some commit reachable
# from the remote ref, at the path the conflict copy shadows. Anything else is
# reported and left alone for a human to diff.
#
#   scripts/clean-sync-conflicts.sh [--apply] [--ref origin/main]
#
# Default is a dry run.
set -euo pipefail

APPLY=0
REF="origin/main"
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --ref) REF="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

cd "$(git rev-parse --show-toplevel)"
git rev-parse --quiet --verify "$REF" >/dev/null || { echo "!! no such ref: $REF" >&2; exit 1; }

# --exclude-standard is deliberately absent: .gitignore hides these by design,
# and hidden is exactly the state this script exists to clean up.
mapfile -t conflicts < <(git ls-files --others -- ':(glob)**/*.sync-conflict-*' | sort)
[ ${#conflicts[@]} -eq 0 ] && { echo "no conflict copies"; exit 0; }

redundant=(); unique=()
for c in "${conflicts[@]}"; do
  base=$(sed -E 's/\.sync-conflict-[0-9]{8}-[0-9]{6}-[A-Z0-9]+//' <<<"$c")
  hash=$(git hash-object "$c")
  # Collect first, match second. Piping straight into `grep -q` makes grep exit
  # on the first hit, which SIGPIPEs the upstream loop, which under `pipefail`
  # reads as a failed pipeline -- so every match would look like a miss.
  seen=$(git rev-list "$REF" -- "$base" \
      | while read -r rev; do git rev-parse -q --verify "$rev:$base" || true; done)
  if grep -qxF "$hash" <<<"$seen"; then
    redundant+=("$c")
  else
    unique+=("$c")
  fi
done

for c in "${unique[@]}"; do
  base=$(sed -E 's/\.sync-conflict-[0-9]{8}-[0-9]{6}-[A-Z0-9]+//' <<<"$c")
  echo "KEEP    $c"
  echo "        content is in no $REF commit; diff it against $base yourself"
done

if [ "$APPLY" -eq 1 ]; then
  [ ${#redundant[@]} -gt 0 ] && rm -- "${redundant[@]}"
  echo "deleted ${#redundant[@]} redundant, kept ${#unique[@]} for review"
else
  for c in "${redundant[@]}"; do echo "DELETE  $c"; done
  echo "dry run: ${#redundant[@]} redundant, ${#unique[@]} to review. Re-run with --apply."
fi
