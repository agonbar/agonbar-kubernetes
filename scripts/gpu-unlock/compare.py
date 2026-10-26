"""Diff two bench.sh runs: python3 compare.py stock unlocked"""
import json
import os
import sys
from pathlib import Path

res = Path(__file__).parent / "results"
a, b = sys.argv[1], sys.argv[2]

ga = json.loads((res / f"{a}-gemm.json").read_text())
gb = json.loads((res / f"{b}-gemm.json").read_text())
print(f"{'TFLOPS':34} {a:>10} {b:>10}  delta")
for k, va in ga["tflops"].items():
    vb = gb["tflops"][k]
    print(f"{k:34} {va:10.1f} {vb:10.1f}  {(vb / va - 1) * 100:+6.1f}%")


def llama(label):
    rows = {}
    for r in json.loads((res / f"{label}-llama.json").read_text()):
        test = f"pp{r['n_prompt']}" if r["n_prompt"] else f"tg{r['n_gen']}"
        rows[(os.path.basename(r["model_filename"])[:24], r["n_ubatch"], test)] = r["avg_ts"]
    return rows


la, lb = llama(a), llama(b)
print(f"\n{'llama-bench tok/s':34} {a:>10} {b:>10}  delta")
for key, va in la.items():
    vb = lb.get(key)
    if vb:
        name = f"{key[0]} ub{key[1]} {key[2]}"
        print(f"{name:34} {va:10.1f} {vb:10.1f}  {(vb / va - 1) * 100:+6.1f}%")
