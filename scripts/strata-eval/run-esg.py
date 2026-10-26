#!/usr/bin/env python3
"""Run esg_eval.py against one target from targets.py.

  run-esg.py strata-nothink [--tag r1] [--only <case-id-prefix>] [--max-tokens N]
  run-esg.py strata-nothink+qwen-instruct --tag r1     # a sampling preset (targets.SAMPLING)

Results go to /mnt/strata/eval/esg-cases/_results/<target>[-<tag>].json (next to the cases,
never into this repo: the answers quote company data). Resumable (--resume): an existing
file's cases are kept whatever produced them, so give every configuration and every repeat
its own target name or --tag.
"""
import os, subprocess, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import targets

EVAL = "/mnt/strata/eval"

if len(sys.argv) < 2:
    raise SystemExit(__doc__)
t = targets.get(sys.argv[1])
rest = sys.argv[2:]
tag = ""
if "--tag" in rest:
    i = rest.index("--tag")
    tag, rest = "-" + rest[i + 1], rest[:i] + rest[i + 2:]
out = os.path.join(EVAL, "esg-cases/_results", t["name"] + tag + ".json")
cmd = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "esg_eval.py"),
       "--base-url", t["root"] + "/v1", "--model", t["model"], "--cases-dir", os.path.join(EVAL, "esg-cases"),
       "--out", out, "--resume"]
if t["extra"]:
    cmd += ["--extra-body", json.dumps(t["extra"])]
env = dict(os.environ, OPENAI_API_KEY=t["key"])
os.execvpe(cmd[0], cmd + rest, env)
