#!/usr/bin/env bash
# Copies the two API keys the strata-eval scripts need into ~/.cache/strata-eval (0600),
# without printing them. Run on work-vm-00.
set -euo pipefail
d=~/.cache/strata-eval
mkdir -p "$d" && chmod 700 "$d"
cat /run/agenix/llm-api-key > "$d/gemma.key"
kubectl --context sensei-ovh -n dev get secret chino-external-env -o jsonpath='{.data.LITELLM_API_KEYS}' \
  | base64 -d | python3 -c 'import sys,json;print(json.load(sys.stdin)["ESG"])' > "$d/litellm.key"
chmod 600 "$d"/*.key
wc -c "$d"/*.key
