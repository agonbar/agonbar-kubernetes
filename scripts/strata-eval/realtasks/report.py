#!/usr/bin/env python3
"""Tables from results/*.jsonl (verifier) and rubric.json (Claude's 0-3 grade of each diff).

  report.py > results/REPORT.md

rubric.json: {"<contender>": {"<task>": [score, "one-line reason"]}}. The last result line per
contender and task wins. Wall time is the agent's own run, without the verifier.
"""
import collections, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tasks as T

ORDER = ["strata-nothink", "strata-think", "gemma", "claude-sonnet"]


def load():
    res = {}
    for c in ORDER:
        p = os.path.join(HERE, "results", c + ".jsonl")
        if os.path.exists(p):
            for l in open(p):
                r = json.loads(l)
                res.setdefault(c, {})[r["task"]] = r
    rub = json.load(open(os.path.join(HERE, "rubric.json"))) if os.path.exists(os.path.join(HERE, "rubric.json")) else {}
    return res, rub


def cell(r, s):
    if not r:
        return "-"
    flag = "✓" if r["pass"] else "✗"
    extra = " cap" if r.get("capHit") else (" t/o" if r.get("timedOut") else "")
    return "%s %s %dm%s" % (flag, s[0] if s else "?", round(r["wallSec"] / 60), extra)


def main():
    res, rub = load()
    cs = [c for c in ORDER if c in res]
    print("| tarea | cat | dif | " + " | ".join(cs) + " |")
    print("|---|---|---|" + "---|" * len(cs))
    for tid, t in T.TASKS.items():
        print("| %s | %s | %s | %s |" % (tid, t["cat"], t["diff"], " | ".join(
            cell(res[c].get(tid), rub.get(c, {}).get(tid)) for c in cs)))
    print("\nCelda: verificador ✓/✗, nota de rúbrica 0-3, minutos de agente; cap = tope de llamadas, t/o = tope de tiempo.\n")
    for key in ("cat", "diff"):
        groups = collections.OrderedDict()
        for tid, t in T.TASKS.items():
            groups.setdefault(t[key], []).append(tid)
        print("| %s | n | " % key + " | ".join(cs) + " |")
        print("|---|---|" + "---|" * len(cs))
        for g, tids in groups.items():
            row = []
            for c in cs:
                rs = [res[c].get(x) for x in tids if res[c].get(x)]
                sc = [rub.get(c, {}).get(x, [None])[0] for x in tids if rub.get(c, {}).get(x)]
                row.append("%d/%d ✓, %.1f" % (sum(r["pass"] for r in rs), len(rs), sum(sc) / len(sc)) if sc else
                           "%d/%d ✓" % (sum(r["pass"] for r in rs), len(rs)))
            print("| %s | %d | %s |" % (g, len(tids), " | ".join(row)))
        print()
    print("| contendiente | ✓ | rúbrica media | min totales | llamadas | prompt tok | cacheados | salida tok | topes |")
    print("|---|---|---|---|---|---|---|---|---|")
    for c in cs:
        rs = list(res[c].values())
        w = lambda k: sum((r.get("wire") or {}).get(k) or 0 for r in rs)
        sc = [v[0] for v in rub.get(c, {}).values()]
        print("| %s | %d/%d | %s | %d | %d | %d | %d | %d | %d |" % (
            c, sum(r["pass"] for r in rs), len(rs), "%.2f" % (sum(sc) / len(sc)) if sc else "-",
            round(sum(r["wallSec"] for r in rs) / 60), w("calls"), w("promptTok"), w("cachedTok"), w("outTok"),
            sum(1 for r in rs if r.get("capHit") or r.get("timedOut"))))


if __name__ == "__main__":
    main()
