#!/usr/bin/env python3
"""Long-context question set over a real document: this repo's own manifests.

  longctx.py TARGET --chars 600000 [--max-tokens 8192]

Bundles the tracked YAML under deployments/ and system/ (SealedSecrets skipped: pure
ciphertext), each file headed by its path, cut at --chars. Then asks, in one request:
  q1..q5  the container image of 5 Deployments picked at 10/30/50/70/90% depth (lookup)
  q6      every Deployment pinned to one node via kubernetes.io/hostname, the node with
          about 6 of them (aggregation over the whole bundle)
Answers are computed from the same bundle text, so the check is exact.
Results: results/longctx-<target>-<chars>.json
"""
import argparse, json, os, re, subprocess, sys, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import targets

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))


def bundle(max_chars):
    files = subprocess.run(["git", "ls-files", "deployments", "system"], cwd=REPO, capture_output=True, text=True).stdout.split()
    parts, n = [], 0
    for f in sorted(files):
        if not f.endswith((".yaml", ".yml")) or "sealedsecret" in f.lower():
            continue
        txt = open(os.path.join(REPO, f), errors="replace").read()
        if "kind: SealedSecret" in txt:
            continue
        block = "=== FILE: %s ===\n%s\n" % (f, txt)
        if n + len(block) > max_chars:
            break
        parts.append((f, block))
        n += len(block)
    return parts


def deployments(parts):
    """(name, file, offset, images, pinned host) for every Deployment document."""
    out, off = [], 0
    for f, block in parts:
        for doc in re.split(r"(?m)^---\s*$", block):
            if re.search(r"(?m)^kind:\s*Deployment\s*$", doc):
                m = re.search(r"(?m)^metadata:\s*\n(?:\s+.*\n)*?\s+name:\s*(\S+)", doc)
                imgs = re.findall(r"(?m)^\s+(?:-\s+)?image:\s*[\"']?([^\s\"']+)", doc)
                h = re.search(r"kubernetes\.io/hostname:\s*[\"']?([\w.-]+)", doc)
                if m:
                    out.append({"name": m.group(1), "file": f, "offset": off, "images": imgs, "host": h.group(1) if h else None})
        off += len(block)
    return out


def questions(parts):
    total = sum(len(b) for _, b in parts)
    deps = deployments(parts)
    names = [d["name"] for d in deps]
    allimgs = [i for d in deps for i in d["images"]]
    qs, used = [], set()
    for depth in (0.1, 0.3, 0.5, 0.7, 0.9):
        cands = [d for d in deps if names.count(d["name"]) == 1 and len(d["images"]) == 1 and allimgs.count(d["images"][0]) == 1
                 and d["name"] not in used]
        if not cands:
            continue
        d = min(cands, key=lambda d: abs(d["offset"] / total - depth))
        used.add(d["name"])
        qs.append({"id": "q%d" % (len(qs) + 1), "kind": "lookup", "depth": round(d["offset"] / total, 2),
                   "q": "What is the full container image reference (registry/name:tag exactly as written) of the Deployment named `%s`?" % d["name"],
                   "answer": d["images"][0]})
    hosts = {}
    for d in deps:
        if d["host"]:
            hosts.setdefault(d["host"], set()).add(d["name"])
    host = min(sorted(hosts), key=lambda h: abs(len(hosts[h]) - 6))
    qs.append({"id": "q%d" % (len(qs) + 1), "kind": "aggregate",
               "q": "List the metadata.name of every Deployment pinned to node `%s` (via `kubernetes.io/hostname`)." % host,
               "answer": sorted(hosts[host])})
    return qs


def norm_img(s):
    return str(s).strip().strip("`'\"")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target")
    ap.add_argument("--chars", type=int, default=600000)
    ap.add_argument("--max-tokens", type=int, default=8192)
    ap.add_argument("--timeout", type=float, default=3600)
    ap.add_argument("--dry", action="store_true", help="print the questions and answers, send nothing")
    a = ap.parse_args()
    parts = bundle(a.chars)
    qs = questions(parts)
    doc = "".join(b for _, b in parts)
    if a.dry:
        print("chars=%d files=%d" % (len(doc), len(parts)))
        print(json.dumps(qs, indent=1))
        return
    t = targets.get(a.target)
    ask = "\n".join("%s. %s" % (q["id"], q["q"]) for q in qs)
    prompt = ("Below is a bundle of Kubernetes manifests from one repository, each file headed by `=== FILE: path ===`.\n\n"
              + doc + "\n\nAnswer these questions using only the bundle above:\n" + ask +
              "\n\nReply with a single JSON object mapping each question id to its answer: a string for lookups, "
              "a JSON array of names for list questions. No other text.")
    body = dict({"model": t["model"], "temperature": 0, "max_tokens": a.max_tokens,
                 "messages": [{"role": "user", "content": prompt}]}, **t["extra"])
    req = urllib.request.Request(t["root"] + "/v1/chat/completions", json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "Authorization": "Bearer " + t["key"]})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=a.timeout) as r:
            d = json.load(r)
        err = None
    except Exception as e:
        d, err = {}, "%s: %s" % (type(e).__name__, getattr(e, "read", lambda: b"")()[:300] or e)
    lat = time.time() - t0
    msg = ((d.get("choices") or [{}])[0].get("message") or {})
    text = msg.get("content") or ""
    s = re.sub(r"(?s)<think>.*?</think>", "", text)
    i, j = s.find("{"), s.rfind("}")
    try:
        ans = json.loads(s[i:j + 1]) if i >= 0 else {}
    except ValueError:
        ans = {}
    res = []
    for q in qs:
        got = ans.get(q["id"])
        if q["kind"] == "lookup":
            ok = got is not None and norm_img(got) == q["answer"]
            res.append({"id": q["id"], "depth": q["depth"], "ok": ok, "want": q["answer"], "got": got})
        else:
            gs = {str(x).strip() for x in got} if isinstance(got, list) else set()
            ws = set(q["answer"])
            res.append({"id": q["id"], "ok": gs == ws, "want": sorted(ws), "got": sorted(gs),
                        "missing": sorted(ws - gs), "extra": sorted(gs - ws)})
    u = d.get("usage") or {}
    summ = {"target": t["name"], "chars": len(doc), "promptTokens": u.get("prompt_tokens"), "completionTokens": u.get("completion_tokens"),
            "latencySec": round(lat, 1), "lookupOk": "%d/%d" % (sum(r["ok"] for r in res if "depth" in r), sum(1 for r in res if "depth" in r)),
            "aggregateOk": [r["ok"] for r in res if "depth" not in r], "validJson": bool(ans), "error": err,
            "finish": (d.get("choices") or [{}])[0].get("finish_reason"), "timings": d.get("timings")}
    print(json.dumps(summ))
    for r in res:
        print(" ", json.dumps(r, ensure_ascii=False)[:300])
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "longctx-%s-%d.json" % (t["name"], a.chars))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump({"summary": summ, "results": res, "rawAnswer": text[:4000], "reasoningChars": len(msg.get("reasoning_content") or "")},
              open(out, "w"), indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
