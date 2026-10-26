#!/usr/bin/env python3
"""Does sampling slow Strata down? Decode speed and MTP draft acceptance per sampling preset.

  sampling_speed.py [--presets greedy,qwen-instruct,qwen-think] [--rounds 3] [--max-tokens 400]

Sends the same prompts (a code answer, a prose answer, a JSON extraction) with each preset of
targets.SAMPLING, thinking off, interleaved round by round so a warmer expert cache does not
favour one preset. Reads the figures Strata returns in `timings` (OpenAI API, non-stream):
predicted_per_second and draft_n / draft_n_accepted. One request at a time.
Results: results/sampling-speed.json and a table on stdout.
"""
import argparse, json, os, statistics, sys, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import targets

PROMPTS = {
    "code": "Write a Python module that parses a CSV of bank transactions (date, description, amount, currency), "
            "converts every amount to EUR with a rates dict, and prints monthly totals. Code only, with docstrings.",
    "prose": "Explain to a new operations engineer how a Kubernetes rolling update works, what maxSurge and "
             "maxUnavailable do, and how to roll back. Plain prose, about 300 words.",
    "json": "Extract every company, year and CO2 figure from this text as a JSON list of objects with keys company, "
            "year, scope, tonnes. Text: In 2024 Acme Steel emitted 1,204,500 t CO2e in scope 1 and 310,220 t in "
            "scope 2 (market based); in 2023 the figures were 1,250,010 t and 335,400 t. Beta Cement reports "
            "scope 1 of 845 kt and scope 3 of 2.1 Mt for 2024, and 902 kt scope 1 for 2023. Gamma Glass did not "
            "publish 2024 data; its 2023 scope 1 was 98,300 tCO2e and scope 2 location based 41,200 tCO2e.",
}


def ask(t, prompt, sampling, max_tokens, timeout):
    body = dict({"model": t["model"], "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens,
                 "strata_checkpoint": False}, **t["extra"])
    body.update(sampling)
    req = urllib.request.Request(t["root"] + "/v1/chat/completions", json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "Authorization": "Bearer " + t["key"]})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    tm = d.get("timings") or {}
    return {"wall": round(time.time() - t0, 2), "tokens": tm.get("predicted_n"), "tokS": tm.get("predicted_per_second"),
            "drafts": tm.get("draft_n"), "accepted": tm.get("draft_n_accepted"), "finish": d["choices"][0]["finish_reason"]}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", default="strata-nothink")
    ap.add_argument("--presets", default="greedy,qwen-instruct,qwen-instruct-nopp,qwen-think")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--max-tokens", type=int, default=400)
    ap.add_argument("--timeout", type=float, default=600)
    a = ap.parse_args()
    t = targets.get(a.target)
    presets = a.presets.split(",")
    rows = []
    for rnd in range(a.rounds):
        for kind, prompt in PROMPTS.items():
            for p in (presets if rnd % 2 == 0 else presets[::-1]):
                r = dict(ask(t, prompt, targets.SAMPLING[p], a.max_tokens, a.timeout), preset=p, prompt=kind, round=rnd)
                rows.append(r)
                print("r%d %-6s %-20s %4s tok %6.1f tok/s drafts %s/%s %s" % (rnd, kind, p, r["tokens"], r["tokS"] or 0,
                      r["accepted"], r["drafts"], r["finish"]), flush=True)
    print("\n| preset | requests | median tok/s | drafts accepted |")
    print("|---|---|---|---|")
    summ = {}
    for p in presets:
        rs = [r for r in rows if r["preset"] == p]
        acc, dr = sum(r["accepted"] or 0 for r in rs), sum(r["drafts"] or 0 for r in rs)
        toks = sum(r["tokens"] or 0 for r in rs)
        summ[p] = {"requests": len(rs), "medianTokS": statistics.median(r["tokS"] for r in rs if r["tokS"]),
                   "accepted": acc, "drafts": dr, "acceptRate": round(acc / dr, 3) if dr else None, "tokens": toks}
        print("| %s | %d | %.1f | %s |" % (p, len(rs), summ[p]["medianTokS"], "%d/%d (%.1f%%)" % (acc, dr, 100.0 * acc / dr) if dr else "-"))
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "sampling-speed.json")
    json.dump({"summary": summ, "rows": rows, "maxTokens": a.max_tokens, "target": a.target,
               "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, open(out, "w"), indent=1)


if __name__ == "__main__":
    main()
