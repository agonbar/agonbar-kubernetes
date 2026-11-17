#!/usr/bin/env bash
# Bump one image (or Helm chart targetRevision) through GitOps and prove the
# workloads still come up.
#
#   ./bump-image.sh <file> <old-string> <new-string> <workload>... [-- <url>]
#
#   <workload>  ns/kind/name, e.g. sure/deploy/sure-web or kube-system/ds/kube-vip
#   <url>       optional; must answer 2xx/3xx after the rollout
#
# Steps: replace old->new in <file> (must match exactly once per line it hits),
# commit, push, hard-refresh the ArgoCD app whose source path holds <file>,
# wait until each workload's spec carries <new-string> (skipped for chart
# bumps, see SKIP_SPEC=1), wait for the rollout, then watch the pods for
# 60 s and fail if any restarts or goes unready. It never reverts: on failure
# it prints the commit so you can `git revert` it.
set -euo pipefail
CTX=${CTX:-lamg}
K="kubectl --context $CTX"
cd "$(git rev-parse --show-toplevel)"

file=$1 old=$2 new=$3; shift 3
workloads=() url=
while [ $# -gt 0 ]; do
  if [ "$1" = -- ]; then url=$2; break; fi
  workloads+=("$1"); shift
done

grep -qF -- "$old" "$file" || { echo "'$old' not in $file" >&2; exit 1; }
OLD=$old NEW=$new perl -pi -e 's/\Q$ENV{OLD}\E/$ENV{NEW}/g' "$file"
git add "$file"
git commit -q -m "$(basename "$(dirname "$file")"): bump ${old##*/} -> ${new##*/}

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push -q
commit=$(git rev-parse --short HEAD)
echo "pushed $commit"

dir=$(dirname "$file")
app=$(grep -l "path: '\?$dir'\?\s*$" system/global-argocd-apps/*.yaml | head -1 | xargs -r yq '.metadata.name' 2>/dev/null || true)
if [ -n "$app" ]; then
  $K -n argocd annotate app "$app" argocd.argoproj.io/refresh=hard --overwrite >/dev/null
  echo "refreshed argocd app $app"
else
  echo "no argocd app found for $dir, relying on autosync" >&2
fi

fail() { echo "FAIL: $*  (revert with: git revert --no-edit $commit && git push)" >&2; exit 1; }

for w in "${workloads[@]}"; do
  IFS=/ read -r ns kind name <<<"$w"
  if [ "${SKIP_SPEC:-0}" != 1 ]; then
    for _ in $(seq 60); do
      $K -n "$ns" get "$kind/$name" -o json | grep -qF -- "$new" && break
      sleep 10
    done
    $K -n "$ns" get "$kind/$name" -o json | grep -qF -- "$new" || fail "$w never got $new"
  fi
  $K -n "$ns" rollout status "$kind/$name" --timeout=600s || fail "$w rollout"
done

# 60 s soak: restarts and readiness of every pod the workloads select
snap() {
  for w in "${workloads[@]}"; do
    IFS=/ read -r ns kind name <<<"$w"
    sel=$($K -n "$ns" get "$kind/$name" -o jsonpath='{.spec.selector.matchLabels}' |
      jq -r 'to_entries|map("\(.key)=\(.value)")|join(",")')
    $K -n "$ns" get pods -l "$sel" --field-selector=status.phase!=Succeeded,status.phase!=Failed -o json |
      jq -r '.items[] | "\(.metadata.name) \([.status.containerStatuses[]?.restartCount]|add) \([.status.containerStatuses[]?.ready]|all)"'
  done | sort
}
before=$(snap); sleep 60; after=$(snap)
echo "$after"
[ "$before" = "$after" ] || fail "pods changed during soak"
grep -q ' false$' <<<"$after" && fail "unready pods"

if [ -n "$url" ]; then
  code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 20 "$url")
  [[ $code =~ ^[23] ]] || fail "$url answered $code"
  echo "$url -> $code"
fi
echo "OK $commit"
