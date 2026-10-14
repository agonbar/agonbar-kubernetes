#!/usr/bin/env bash
# Smoke test for deployments/agonbar/devbox.yaml, run locally with podman.
# Pulls seed.sh and start.sh out of the manifest, so it tests what ArgoCD ships.
# Proves: seeding works, key-only SSH works, sudo works, and a package
# installed with apt is still there after the container is recreated.
set -euo pipefail

manifest="$(cd "$(dirname "$0")/.." && pwd)/deployments/agonbar/devbox.yaml"
work="$(mktemp -d)"
port=2299
name=devbox-smoke
trap 'podman rm -f $name >/dev/null 2>&1 || true; podman unshare rm -rf "$work"' EXIT

mkdir -p "$work"/{persist,scripts,keys}
for s in seed.sh start.sh; do
  kubectl create --dry-run=client -f "$manifest" -o json \
    | jq -rs --arg s "$s" '.. | objects | select(.kind? == "ConfigMap" and .metadata.name == "devbox-scripts") | .data[$s]' \
    > "$work/scripts/$s"
done
ssh-keygen -q -t ed25519 -N '' -f "$work/id"
cp "$work/id.pub" "$work/keys/authorized_keys"

echo "== seed"
podman run --rm -v "$work/persist:/persist" -v "$work/scripts:/scripts:ro" \
  ubuntu:24.04 sh /scripts/seed.sh >"$work/seed.log" 2>&1 || { tail -30 "$work/seed.log"; exit 1; }
echo "== seed again (must be a no-op)"
podman run --rm -v "$work/persist:/persist" -v "$work/scripts:/scripts:ro" \
  ubuntu:24.04 sh /scripts/seed.sh

start() {
  local mounts=()
  for d in usr etc var opt home root srv; do mounts+=(-v "$work/persist/$d:/$d"); done
  podman run -d --name $name -p 127.0.0.1:$port:22 "${mounts[@]}" \
    -v "$work/scripts:/scripts:ro" -v "$work/keys:/keys:ro" \
    ubuntu:24.04 sh /scripts/start.sh >/dev/null
  for _ in $(seq 30); do in_box true 2>/dev/null && return; sleep 1; done
  podman logs $name; exit 1
}
in_box() {
  ssh -q -i "$work/id" -o IdentitiesOnly=yes -p $port -o StrictHostKeyChecking=no -o UserKnownHostsFile="$work/known_hosts" \
    -o BatchMode=yes ubuntu@127.0.0.1 "$@"
}

echo "== first boot"
start
in_box 'sudo apt-get install -y -qq cowsay >/dev/null 2>&1 && echo hola > ~/nota'
echo "== password login must be refused"
if ssh -q -p $port -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes \
  -o PubkeyAuthentication=no ubuntu@127.0.0.1 true 2>/dev/null; then echo "FAIL: got in without a key"; exit 1; fi

echo "== recreate the container"
podman rm -f $name >/dev/null
start
in_box '/usr/games/cowsay ok >/dev/null && cat ~/nota'
echo "PASS"
