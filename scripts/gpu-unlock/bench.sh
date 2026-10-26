#!/usr/bin/env bash
# Measure the RTX 3090 on work-vm-00 before and after gpu-unlock.
#   scripts/gpu-unlock/bench.sh <label>     e.g. stock, unlocked
# Writes results/<label>-{gemm,llama}.json next to this script. Needs
# llama-rpc-standalone stopped (it holds the 3090's VRAM). Same commands both
# times, so `compare.py stock unlocked` is a straight diff.
set -euo pipefail

label=${1:?usage: bench.sh <label>}
here=$(cd "$(dirname "$0")" && pwd)
out=$here/results
mkdir -p "$out"

uuid=GPU-458073c4-df24-7d92-631c-605117c5a3a5   # the 3090, as in work-vm-00's config (CDI name "1")
image=quay.io/docling-project/docling-serve-cu128:v1.18.0
llama=$(dirname "$(readlink -f "$(command -v llama-server 2>/dev/null || echo /nix/store/adr96qh3pnji7skncz3d39j4cwfqhp80-llama-cpp-9608/bin/llama-server)")")
models=${MODELS:-"$HOME/llms/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf $HOME/llms/bench/Qwen3-4B-BF16.gguf"}
sudo=/run/wrappers/bin/sudo

if systemctl is-active -q llama-rpc-standalone; then
  echo "stop llama-rpc-standalone first" >&2
  exit 1
fi

echo "== gemm ($label)"
$sudo podman run --rm --device nvidia.com/gpu=1 -v "$here/gemm.py:/gemm.py:ro" \
  --entrypoint python3 "$image" /gemm.py | tee "$out/$label-gemm.json"

echo "== llama-bench ($label)"
# -fa 1 like production; ub 512 is production's, 2048 is the big-batch prefill case.
# shellcheck disable=SC2086
CUDA_VISIBLE_DEVICES=$uuid "$llama/llama-bench" $(printf -- '-m %s ' $models) \
  -ngl 999 -fa 1 -p 512,4096 -n 128 -ub 512,2048 -r 3 -o json > "$out/$label-llama.json"
python3 - "$out/$label-llama.json" <<'EOF'
import json, sys, os
for r in json.load(open(sys.argv[1])):
    test = f"pp{r['n_prompt']}" if r['n_prompt'] else f"tg{r['n_gen']}"
    print(f"{os.path.basename(r['model_filename'])[:34]:34} ub={r['n_ubatch']:<5} {test:7} {r['avg_ts']:9.1f} t/s")
EOF
