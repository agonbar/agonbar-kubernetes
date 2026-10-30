#!/usr/bin/env bash
# Tests gateway.py, then builds ghcr.io/agonbar/strata-gateway:<tag> and pushes it.
# Uso: scripts/strata-gateway/build.sh [tag]   (por defecto, el hash corto de gateway.py + Dockerfile)
# Necesita docker con login en ghcr.io (write:packages) y nix para los tests.
# Luego: poner la tag en deployments/strata/deployment.yaml (contenedor gateway).
set -euo pipefail
cd "$(dirname "$0")"
TAG="${1:-$(cat gateway.py Dockerfile | sha256sum | cut -c1-8)}"
IMAGE="ghcr.io/agonbar/strata-gateway:$TAG"
nix-shell -p 'python3.withPackages(p: [p.aiohttp])' --run 'python test_gateway.py'
docker build -t "$IMAGE" .
docker push "$IMAGE"
echo "$IMAGE"
