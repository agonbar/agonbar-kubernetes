#!/usr/bin/env python3
"""Print the comparison tables (markdown) from everything the other scripts left behind.

  summarize.py

Reads /mnt/strata/eval/esg-cases/_results/*.json (ESG, summary fields only) and
results/{toolcall,agent,longctx,vision,sampling-speed}*.json. Runs repeated with --tag rN
(run-strata.sh sampling-*) also get one row per configuration: mean, sd and range.
"""
import glob, json, os, re, statistics

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


def spread(xs):
    m = statistics.mean(xs)
    return "%.1f ± %.1f (%s-%s)" % (m, statistics.stdev(xs) if len(xs) > 1 else 0, min(xs), max(xs))


def frac(x):
    return int(x.split("/")[0])


def repeats():
    print("## Repeated runs (--tag rN): mean ± sd (min-max)\n")
    groups = {}
    for p in glob.glob(os.path.join(ESG, "*-r[0-9].json")):
        d = load(p)
        if d and "summary" in d:
            groups.setdefault(re.sub(r"-r\d+\.json$", "", os.path.basename(p)), []).append(d["summary"])
    print("| ESG config | runs | verdicts with data /15 | scope values /34 | no-data correct /5 | median s/case | decode tok/s | drafts accepted |")
    print("|---|---|---|---|---|---|---|---|")
    for k, ss in sorted(groups.items()):
        acc = [frac(s.get("draftsAccepted", "0/0")) / max(1, int(s.get("draftsAccepted", "0/1").split("/")[1])) for s in ss]
        print("| %s | %d | %s | %s | %s | %s | %s | %.1f%% |" % (k, len(ss), spread([frac(s["positiveVerdictCorrect"]) for s in ss]),
              spread([frac(s["scopeValuesOk"]) for s in ss]), spread([frac(s["noDataVerdictCorrect"]) for s in ss]),
              spread([s["medianLatencySec"] for s in ss]), spread([s.get("medianDecodeTokS") or 0 for s in ss]),
              100 * statistics.mean(acc)))
    print()
    groups = {}
    for p in glob.glob(os.path.join(HERE, "results/toolcall-*-r[0-9].json")):
        d = load(p)
        groups.setdefault(re.sub(r"-r\d+\.json$", "", os.path.basename(p))[len("toolcall-"):], []).append(d)
    print("| tools config | runs | pass /21 | median s | cases failed (times) |")
    print("|---|---|---|---|---|")
    for k, ds in sorted(groups.items()):
        fails = {}
        for d in ds:
            for r in d["results"]:
                if not r["pass"]:
                    fails[r["id"]] = fails.get(r["id"], 0) + 1
        print("| %s | %d | %s | %s | %s |" % (k, len(ds), spread([d["summary"]["pass"] for d in ds]),
              spread([d["summary"]["medianLatencySec"] for d in ds]), ", ".join("%s (%d)" % kv for kv in sorted(fails.items())) or "-"))
    print()
    d = load(os.path.join(HERE, "results/sampling-speed.json"))
    if d:
        print("## Decode speed and MTP drafts per sampling preset (sampling_speed.py, %d tokens per answer)\n" % d["maxTokens"])
        print("| preset | requests | median tok/s | drafts accepted |")
        print("|---|---|---|---|")
        for k, s in d["summary"].items():
            print("| %s | %d | %.1f | %s/%s (%.1f%%) |" % (k, s["requests"], s["medianTokS"], s["accepted"], s["drafts"], 100 * (s["acceptRate"] or 0)))
        print()


if __name__ == "__main__":
    esg(); toolcall(); agent(); longctx(); vision(); repeats()
