#!/usr/bin/env bash
# Runs the whole battery against Strata, one request at a time, cheapest-signal first.
# Needs the port-forward up in another shell:
#   kubectl --context lamg port-forward -n strata svc/strata 18091:8080
# Usage: run-strata.sh [step...]   steps: smoke esg-nothink tools-nothink agent agent-nothink longctx tools-think esg-think vision
#        run-strata.sh sampling-speed sampling-tools sampling-esg sampling-agent   (not in the default list:
#        greedy against the model card's sampling presets, ~2.5 h)
# Vision goes last: Strata loads the encoder on the first image and keeps it ~120 s,
# taking ~2 GiB of expert cache, which would slow whatever runs next.
# Logs: /tmp/strata-eval-logs/<step>.log ; results as each script documents.
set -uo pipefail
cd "$(dirname "$0")"
L=/tmp/strata-eval-logs
mkdir -p "$L"
steps=("$@")
[ ${#steps[@]} -eq 0 ] && steps=(smoke esg-nothink tools-nothink agent agent-nothink longctx tools-think esg-think vision)

for s in "${steps[@]}"; do
  echo "== $(date +%T) $s"
  case $s in
    smoke)
      curl -sf -m 10 "${STRATA_URL:-http://127.0.0.1:18091}/health" && echo
      curl -s -m 600 "${STRATA_URL:-http://127.0.0.1:18091}/v1/models" | head -c 400; echo ;;
    esg-nothink)   ./run-esg.py strata-nothink > "$L/$s.log" 2>&1 ;;
    esg-think)     ./run-esg.py strata > "$L/$s.log" 2>&1 ;;
    tools-nothink) ./toolcall_bench.py strata-nothink > "$L/$s.log" 2>&1 ;;
    tools-think)   ./toolcall_bench.py strata --max-tokens 16384 > "$L/$s.log" 2>&1 ;;
    agent)         ./run_agent.py strata --task-timeout 3600 > "$L/$s.log" 2>&1 ;;
    agent-nothink) CLAUDE_CODE_EXTRA_BODY='{"thinking":{"type":"disabled"}}' \
                     ./run_agent.py strata --tag nothink --task-timeout 3600 > "$L/$s.log" 2>&1 ;;
    vision)        ./vision.py strata-nothink > "$L/$s.log" 2>&1 ;;
    # sampled runs differ run to run: repeats, each to its own file (run-esg.py --resume would mix them).
    # Rerunning a step finishes what is missing: a complete file is skipped case by case.
    sampling-speed) ./sampling_speed.py > "$L/$s.log" 2>&1 ;;
    sampling-tools) for r in r1 r2 r3; do for p in greedy qwen-instruct; do
                      ./toolcall_bench.py strata-nothink+$p --tag $r > "$L/$s-$p-$r.log" 2>&1; done; done ;;
    sampling-esg)   for run in greedy:r1 qwen-instruct:r1 qwen-instruct-nopp:r1 qwen-instruct:r2 qwen-instruct-nopp:r2 qwen-instruct:r3 greedy:r2 qwen-instruct-nopp:r3 greedy:r3; do
                      ./run-esg.py strata-nothink+${run%%:*} --tag ${run#*:} > "$L/$s-${run%%:*}-${run#*:}.log" 2>&1; done ;;
    sampling-agent) CLAUDE_CODE_EXTRA_BODY='{"thinking":{"type":"disabled"}}' \
                      ./run_agent.py strata+qwen-instruct --tag nothink --task-timeout 3600 > "$L/$s.log" 2>&1 ;;
    longctx)       ./longctx.py strata-nothink --chars 400000 > "$L/$s-400k.log" 2>&1
                   ./longctx.py strata-nothink --chars 650000 > "$L/$s-650k.log" 2>&1 ;;
    *) echo "unknown step $s" ;;
  esac
  tail -n 3 "$L/$s"*.log 2>/dev/null
done
