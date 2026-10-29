#!/usr/bin/env python3
"""What the junior.ts harness did in each run: finish calls and rejections, scope-guard and budget
blocks, nudges, and the call at which the first plan and the first finish came.

  harness_stats.py [RUN_DIR ...]     default: /tmp/realtasks/runs/strata-harness/*

Reads agent.jsonl (pi --mode json) of each run; prints one markdown row per run.
"""
import glob, json, os, sys


def stats(path):
    s = {"tools": 0, "plan": None, "finish": 0, "rejected": 0, "review": 0, "accepted": None,
         "scope_block": 0, "budget_block": 0, "nudge": 0}
    for line in open(path, errors="replace"):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        typ = ev.get("type")
        if typ == "tool_execution_start":
            s["tools"] += 1
            if ev["toolName"] == "plan" and s["plan"] is None:
                s["plan"] = s["tools"]
            if ev["toolName"] == "finish":
                s["finish"] += 1
        elif typ == "tool_execution_end":
            r = ev.get("result") or {}
            txt = "".join(c.get("text", "") for c in (r.get("content") or []) if isinstance(c, dict))
            if ev["toolName"] == "finish":
                if txt.startswith("finish rechazado"):
                    s["rejected"] += 1
                elif txt.startswith("Revisa tu entrega"):
                    s["review"] += 1
                elif txt.startswith("Entregado"):
                    s["accepted"] = "forzado" if "límite" in txt else "sí"
            elif "no está en tu plan" in txt or "Antes de escribir, llama a plan" in txt:
                s["scope_block"] += 1
            elif "Se acabó el presupuesto" in txt:
                s["budget_block"] += 1
        elif typ == "message_end":
            m = ev.get("message") or {}
            if m.get("customType") == "junior_nudge":
                s["nudge"] += 1
    return s


def main():
    dirs = sys.argv[1:] or sorted(glob.glob("/tmp/realtasks/runs/strata-harness/*"))
    cols = ["tools", "plan", "finish", "rejected", "review", "accepted", "scope_block", "budget_block", "nudge"]
    print("| run | " + " | ".join(cols) + " |")
    print("|---|" + "---|" * len(cols))
    for d in dirs:
        p = os.path.join(d, "agent.jsonl")
        if os.path.exists(p):
            s = stats(p)
            print("| %s | %s |" % (os.path.basename(d.rstrip("/")), " | ".join(str(s[c]) for c in cols)))


if __name__ == "__main__":
    main()
