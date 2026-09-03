#!/bin/sh
# Seal every <dir>/secrets.yaml into <dir>/sealedsecrets.yaml.
#
# Several secrets.yaml files are shorter than the sealedsecrets.yaml next to
# them, because sealed secrets have been added straight to the sealed file
# without the plaintext ever landing locally. Regenerating from a short
# secrets.yaml silently drops the rest, and the old `kubeseal ... > dst` form
# truncated dst before kubeseal even ran, so a failed seal emptied the file too.
#
# So: seal into a temp file, and refuse to install it over one that holds more
# SealedSecret documents. Run with DRY_RUN=1 to see what each pair would do.
set -eu

DIRS="
system/argocd
system/democratic-csi
deployments/agonbar
deployments/aya
deployments/dawarich
deployments/games
deployments/immich
deployments/knowledge
deployments/lamg
deployments/piracy
deployments/trek
deployments/yavoo
"

DRY_RUN="${DRY_RUN:-0}"
failed=0

count_sealed() {
  [ -f "$1" ] || { echo 0; return; }
  grep -c '^kind: SealedSecret' "$1" 2>/dev/null || echo 0
}

for d in $DIRS; do
  src="./$d/secrets.yaml"
  dst="./$d/sealedsecrets.yaml"
  if [ ! -f "$src" ]; then
    echo "skip   $d (no secrets.yaml)"
    continue
  fi

  tmp=$(mktemp)
  if ! kubeseal --context lamg -f "$src" -o yaml --scope cluster-wide > "$tmp" 2>/dev/null; then
    echo "FAIL   $d: kubeseal errored, $dst left untouched"
    rm -f "$tmp"
    failed=1
    continue
  fi

  new=$(count_sealed "$tmp")
  old=$(count_sealed "$dst")
  if [ "$new" -lt "$old" ]; then
    echo "REFUSE $d: $src yields $new sealed secrets, $dst already has $old."
    echo "       Add the missing plaintext to $src, or seal the new secret on its"
    echo "       own and append it to $dst. Overwriting here would drop $(( old - new ))."
    rm -f "$tmp"
    failed=1
    continue
  fi

  if [ "$DRY_RUN" != "0" ]; then
    echo "would   $d ($old -> $new)"
    rm -f "$tmp"
    continue
  fi

  mv "$tmp" "$dst"
  echo "ok     $d ($new sealed secrets)"
done

exit "$failed"
