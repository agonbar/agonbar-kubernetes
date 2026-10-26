#!/usr/bin/env bash
# Prefill time of one ESG-sized document on the 3090: production's gemma, one
# prompt of 32k and one of 196k tokens (Phase-4 prompts run 150-250k), across
# ubatch sizes and KV cache types. Decode is left out: Phase 4 answers a short JSON.
#   scripts/gpu-unlock/esg-prefill.sh <label>       LLAMA=<llama.cpp bin dir> to swap builds
# Needs llama-rpc-standalone stopped. Writes results/esg-prefill-<label>.json.
set -euo pipefail

label=${1:?usage: esg-prefill.sh <label>}
here=$(cd "$(dirname "$0")" && pwd)
out=$here/results/esg-prefill-$label.json
llama=${LLAMA:-/nix/store/adr96qh3pnji7skncz3d39j4cwfqhp80-llama-cpp-9608/bin}
model=$HOME/llms/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf

if systemctl is-active -q llama-rpc-standalone; then
  echo "stop llama-rpc-standalone first" >&2
  exit 1
fi

tmp=$(mktemp -d)
for kv in ${KV:-q4_0 q8_0 f16}; do
  CUDA_VISIBLE_DEVICES=GPU-458073c4-df24-7d92-631c-605117c5a3a5 "$llama/llama-bench" -m "$model" \
    -ngl 999 -fa 1 -n 0 -p 32768,196608 -b 4096 -ub ${UB:-512,2048,4096} \
    -ctk "$kv" -ctv "$kv" -r 1 -o json > "$tmp/$kv.json"
done
python3 - "$out" "$tmp"/*.json <<'EOF'
import json, sys
rows = [r for f in sys.argv[2:] for r in json.load(open(f))]
json.dump(rows, open(sys.argv[1], "w"), indent=1)
for r in rows:
    print(f"kv={r['type_k']:5} ub={r['n_ubatch']:<5} pp{r['n_prompt']:<7} {r['avg_ts']:7.0f} tok/s  {r['n_prompt'] / r['avg_ts']:6.1f} s")
EOF
rm -r "$tmp"
