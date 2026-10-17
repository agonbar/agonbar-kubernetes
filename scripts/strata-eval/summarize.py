#!/usr/bin/env python3
"""Print the comparison tables (markdown) from everything the other scripts left behind.

  summarize.py

Reads /mnt/strata/eval/esg-cases/_results/*.json (ESG, summary fields only) and
results/{toolcall,agent,longctx,vision}-*.json.
"""
import glob, json, os

HERE = os.path.dirname(os.path.abspath(__file__))
ESG = "/mnt/strata/eval/esg-cases/_results"


def load(p):
    try:
        return json.load(open(p))
    except (OSError, ValueError):
        return None


def esg():
    print("## ESG (20 cases: 15 with data / 34 scope values, 5 without data)\n")
    print("A target with fewer than 20 cases ran on a subset (`strata` with reasoning: 6 cases).\n")
    print("| target | valid JSON | verdicts with data | scope values | no-data correct | median s/case | completion tok | errors |")
    print("|---|---|---|---|---|---|---|---|")
    for p in sorted(glob.glob(os.path.join(ESG, "*.json"))):
        d = load(p)
        if not d or "summary" not in d:
            continue
        s = d["summary"]
        print("| %s | %s/%s | %s | %s | %s | %s | %s | %s |" % (os.path.basename(p)[:-5], s["validJson"], s["cases"], s["positiveVerdictCorrect"],
              s["scopeValuesOk"], s["noDataVerdictCorrect"], s["medianLatencySec"], s["completionTokens"], s["errors"]))
    print()


def toolcall():
    print("## Tool calling (21 cases)\n")
    print("| target | API | pass | format errors | request errors | median s | total s | failed cases |")
    print("|---|---|---|---|---|---|---|---|")
    for p in sorted(glob.glob(os.path.join(HERE, "results/toolcall-*.json"))):
        d = load(p)
        s = d["summary"]
        fails = ", ".join(r["id"] for r in d["results"] if not r["pass"])
        print("| %s | %s | %d/%d | %d | %d | %s | %s | %s |" % (s["target"], s["api"], s["pass"], s["total"], s["formatErrors"],
              s["requestErrors"], s["medianLatencySec"], s["totalSec"], fails or "-"))
    print()


def agent():
    print("## Claude Code agent tasks\n")
    print("| target | task | pass | turns | tool calls | tool errors | API errors | wall s | end |")
    print("|---|---|---|---|---|---|---|---|---|")
    for p in sorted(glob.glob(os.path.join(HERE, "results/agent-*.json"))):
        d = load(p)
        for r in d["results"]:
            print("| %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (d["summary"]["target"], r["task"], "yes" if r["pass"] else "NO",
                  r["turns"], r["toolCalls"], r["toolErrors"], r["apiErrors"], r["wallSec"],
                  "timeout" if r["timedOut"] else (r["resultSubtype"] or "rc=%s" % r["rc"])))
    print()


def longctx():
    print("## Long context\n")
    print("| target | chars | prompt tok | lookups ok | aggregate ok | s | finish |")
    print("|---|---|---|---|---|---|---|")
    for p in sorted(glob.glob(os.path.join(HERE, "results/longctx-*.json"))):
        s = load(p)["summary"]
        print("| %s | %s | %s | %s | %s | %s | %s |" % (s["target"], s["chars"], s["promptTokens"], s["lookupOk"], s["aggregateOk"],
              s["latencySec"], s["error"] or s["finish"]))
    print()


def vision():
    print("## Vision (emissions table, 5 values)\n")
    print("| target | values ok | s | completion tok |")
    print("|---|---|---|---|")
    for p in sorted(glob.glob(os.path.join(HERE, "results/vision-*.json"))):
        s = load(p)["summary"]
        print("| %s | %s | %s | %s |" % (s["target"], s["ok"], s["latencySec"], s["completionTokens"]))
    print()


if __name__ == "__main__":
    esg(); toolcall(); agent(); longctx(); vision()
