#!/usr/bin/env python3
"""Run esg_eval.py against one target from targets.py.

  run-esg.py strata-nothink [--only <case-id-prefix>] [--max-tokens N]

Results go to /mnt/strata/eval/esg-cases/_results/<target>.json (next to the cases,
never into this repo: the answers quote company data). Resumable (--resume).
"""
import os, subprocess, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import targets

EVAL = "/mnt/strata/eval"

if len(sys.argv) < 2:
    raise SystemExit(__doc__)
t = targets.get(sys.argv[1])
out = os.path.join(EVAL, "esg-cases/_results", t["name"] + ".json")
cmd = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "esg_eval.py"),
       "--base-url", t["root"] + "/v1", "--model", t["model"], "--cases-dir", os.path.join(EVAL, "esg-cases"),
       "--out", out, "--resume"]
if t["extra"]:
    cmd += ["--extra-body", json.dumps(t["extra"])]
env = dict(os.environ, OPENAI_API_KEY=t["key"])
os.execvpe(cmd[0], cmd + sys.argv[2:], env)
