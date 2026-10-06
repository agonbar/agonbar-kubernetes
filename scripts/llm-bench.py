#!/usr/bin/env python3
"""Same-prompt LLM benchmark for any OpenAI-compatible server (llama-server, Strata, ...).

Runs a decode-bound case (short prompt, long answer) and a prefill-bound case
(~N-token prompt, then the same answer length, i.e. decode at depth), each RUNS times, and prints the median.
Prefers the server's own llama.cpp `timings`; falls back to wall clock + usage.
Each run starts with a fresh nonce so the prompt cache cannot skip prefill.

  scripts/llm-bench.py http://127.0.0.1:8080 --label itx-kyoma --json out.json
"""
import argparse, json, statistics, time, urllib.request, uuid

DECODE_PROMPT = ("Write a detailed, step-by-step explanation of how a hash map handles "
                 "collisions, with a worked example in Python. Do not stop early.")
FILLER = ("The quick brown fox jumps over the lazy dog while the committee reviews "
          "quarterly figures for the northern warehouse. ")


def ask(base, model, prompt, max_tokens, api_key, timeout=1800):
    body = json.dumps({"model": model, "temperature": 0, "max_tokens": max_tokens,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(base.rstrip("/") + "/v1/chat/completions", body,
                                 {"Content-Type": "application/json",
                                  **({"Authorization": f"Bearer {api_key}"} if api_key else {})})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.load(r)
    wall = time.monotonic() - t0
    t, u = resp.get("timings"), resp.get("usage", {})
    if t:
        return {"prompt_n": t["prompt_n"], "prefill_tps": t["prompt_per_second"],
                "gen_n": t["predicted_n"], "decode_tps": t["predicted_per_second"], "wall_s": wall}
    gen = u.get("completion_tokens", 0)
    return {"prompt_n": u.get("prompt_tokens"), "prefill_tps": None,
            "gen_n": gen, "decode_tps": gen / wall if wall else None, "wall_s": wall}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base", help="server root, e.g. http://127.0.0.1:8080")
    ap.add_argument("--model", default="default")
    ap.add_argument("--api-key")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--prefill-tokens", type=int, default=8000, help="approximate prompt size")
    ap.add_argument("--decode-tokens", type=int, default=512)
    ap.add_argument("--timeout", type=int, default=1800, help="per request, seconds (1M prefill takes a while)")
    ap.add_argument("--only", choices=["decode", "prefill"], help="run one case only")
    ap.add_argument("--label", default="")
    ap.add_argument("--json", help="also write raw runs + medians here")
    a = ap.parse_args()

    filler = FILLER * (a.prefill_tokens // 22)  # ~22 tokens per FILLER
    cases = {"decode": (DECODE_PROMPT, a.decode_tokens),
             "prefill": (filler + "\nDescribe this text in detail.", a.decode_tokens)}
    if a.only:
        cases = {a.only: cases[a.only]}

    ask(a.base, a.model, "Say hi.", 8, a.api_key)  # warm-up: load weights, fill caches
    out = {"label": a.label, "base": a.base, "cases": {}}
    for name, (prompt, max_tokens) in cases.items():
        runs = []
        for i in range(a.runs):
            r = ask(a.base, a.model, f"[{uuid.uuid4()}]\n{prompt}", max_tokens, a.api_key, a.timeout)
            runs.append(r)
            print(f"{a.label} {name} #{i + 1}: prompt {r['prompt_n']} tok, "
                  f"prefill {r['prefill_tps'] or 0:.1f} tok/s, gen {r['gen_n']} tok, "
                  f"decode {r['decode_tps'] or 0:.2f} tok/s, wall {r['wall_s']:.1f}s", flush=True)
        med = {k: statistics.median(r[k] for r in runs if r[k] is not None)
               for k in ("prefill_tps", "decode_tps", "wall_s") if any(r[k] is not None for r in runs)}
        out["cases"][name] = {"runs": runs, "median": med}
        print(f"{a.label} {name} MEDIAN: " + ", ".join(f"{k} {v:.2f}" for k, v in med.items()), flush=True)
    if a.json:
        with open(a.json, "w") as f:
            json.dump(out, f, indent=2)


if __name__ == "__main__":
    main()
