#!/usr/bin/env python3
"""Score an OpenAI-compatible endpoint on the ESG Phase 4 CO2 extraction cases.

Each case in --cases-dir carries the exact request the pipeline sends plus the
ground truth. That request is one user message (no system prompt), temperature 0
and response_format json_object, which is what esg-bunseki askChinoPro ->
chino -> llm-worker put on the wire; no max_tokens. Python 3 stdlib only.

  esg_eval.py --base-url http://127.0.0.1:8090/v1 --model strata \\
              --cases-dir esg-cases --out esg-cases/_results/strata.json

The cases (real company data) live only in /mnt/strata/eval/esg-cases on work-vm-00;
this copy of the runner adds --extra-body and is the one run-esg.sh uses.

Cases run one at a time (local servers here serve one request at a time).
--out is rewritten after every case, and --resume skips the ids it already has.
--replay DIR scores saved answers (DIR/<id>.txt) without calling any server.
"""
import argparse, glob, json, os, re, sys, time, urllib.error, urllib.request

SCOPES = ("scope1", "scope2", "scope2LocationBased", "scope3", "scopes1And2")
ALL_VALUE_FIELDS = SCOPES + ("totalCo2",)
TOL = 0.01
SHORT = {"scope1": "s1", "scope2": "s2", "scope2LocationBased": "s2LB", "scope3": "s3", "scopes1And2": "s12"}


# ------------------------------------------------------------------ transport

def ask(base_url, api_key, model, request, timeout, max_tokens, json_mode, extra=None):
    body = dict(request, model=model)
    body.update(extra or {})
    if not json_mode:
        body.pop("response_format", None)
    if max_tokens:
        body["max_tokens"] = max_tokens
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + (api_key or "none")})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        return {"error": "HTTP %d: %s" % (e.code, e.read()[:500].decode("utf8", "replace")), "latency": time.time() - t0}
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e), "latency": time.time() - t0}
    ch = (d.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    u = d.get("usage") or {}
    return {"answer": msg.get("content") or "", "reasoning": msg.get("reasoning_content") or "",
            "finish_reason": ch.get("finish_reason"), "latency": time.time() - t0,
            "prompt_tokens": u.get("prompt_tokens"), "completion_tokens": u.get("completion_tokens")}


# ------------------------------------------------------------------ parsing

def parse_answer(text):
    """Mirror of the pipeline's cheap path: drop thinking, fences and prose around the
    outermost object, trailing commas. No model-based repair (the pipeline would spend
    a Pro call on FIX_JSON_PROMPT here; we count it as invalid JSON instead)."""
    s = re.sub(r"(?s)<think>.*?</think>", "", text or "").strip()
    s = re.sub(r"(?s)^```(?:json)?\s*|\s*```$", "", s).strip()
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j < i:
        return None, "no JSON object"
    s = re.sub(r",(\s*[}\]])", r"\1", s[i:j + 1])
    try:
        v = json.loads(s)
    except ValueError as e:
        return None, str(e)[:120]
    return (v, None) if isinstance(v, dict) else (None, "top level is not an object")


def str_to_float(x):
    """analysis.StrToFloat: the last separator is decimal when the tail has 1-2 digits,
    the separators are mixed, or it is a lone dot; otherwise it is grouping."""
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return float(x)
    if not isinstance(x, str):
        return None
    v = x.replace(" ", "").replace(" ", "").strip()
    v = re.sub(r"[^\d.,\-]", "", v)
    if not v or v in ("-", ".", ","):
        return None
    i = max(v.rfind("."), v.rfind(","))
    if i >= 0:
        intp, frac = v[:i], v[i + 1:]
        digits = intp.replace(".", "").replace(",", "")
        decimal = (1 <= len(frac) <= 2) or digits.lstrip("-0") == "" or ("." in v and "," in v) or (v[i] == "." and v.count(".") == 1)
        v = digits + "." + frac if decimal else v.replace(".", "").replace(",", "")
    try:
        return float(v)
    except ValueError:
        return None


def unit_factor(unit):
    """analysis.UnitToTonnes, simplified: None when the unit says nothing usable."""
    u = (unit or "").lower().replace("co₂", "co2").strip()
    if not u or "/" in u or " per " in u:
        return None
    c = re.sub(r"[ .,]", "", u)
    if c.startswith(("mmt", "mt")) or "million" in u or "millon" in u or "millón" in u:
        return 1e6
    if c.startswith(("kt", "gg")) or "kiloton" in u or "thousand" in u or "miles" in u or "mil " in u:
        return 1e3
    if c.startswith("kg"):
        return 1e-3
    if c.startswith(("metricton", "t")) or "tonelad" in u or "tonne" in u:
        return 1.0
    return None


def value_of(field):
    """(raw number, tonnes or None, unit, sourceIndex); absent when <= 0 ("-1"/"0" mean not found)."""
    if not isinstance(field, dict):
        return None
    raw = str_to_float(field.get("value"))
    if raw is None or raw <= 0:
        return None
    f = unit_factor(field.get("unit"))
    return {"raw": raw, "tonnes": raw * f if f else None, "unit": field.get("unit"), "src": str(field.get("sourceIndex", ""))}


# ------------------------------------------------------------------ scoring

def close(a, b):
    return a is not None and b is not None and abs(a - b) <= TOL * abs(b)


def judge_value(v, truth):
    """ok / unit error / wrong for one extracted value against a truth in tonnes."""
    if v is None:
        return "missing"
    best = v["tonnes"] if v["tonnes"] is not None else v["raw"]
    if close(best, truth):
        return "ok" if v["tonnes"] is not None else "ok_no_unit"
    for k, tag in ((1e3, "x1000"), (1e-3, "/1000"), (1e6, "x1e6"), (1e-6, "/1e6")):
        if close(best, truth * k) or close(v["raw"], truth * k):
            return "unit_" + tag
    return "wrong"


def trusted_sources(resp):
    """Indices the model judged the company's. The pipeline drops values attributed to
    PDFs judged no/unlikely (filterByCompanyConfidence); ungraded indices are kept."""
    graded = {str(s.get("sourceIndex")): str(s.get("isTheCompany", "")).lower()
              for s in resp.get("sources") or [] if isinstance(s, dict)}
    return lambda idx: graded.get(idx, "yes") not in ("no", "unlikely")


def years_of(resp):
    return [y for y in resp.get("years") or [] if isinstance(y, dict)]


def score(case, resp):
    t, kind = case["truth"], case["kind"]
    keep = trusted_sources(resp)
    values = []  # every scope value the model returned, any year
    for y in years_of(resp):
        for f in ALL_VALUE_FIELDS:
            v = value_of(y.get(f))
            if v:
                v.update(year=str(y.get("year", "")), field=f, kept=keep(v["src"]))
                values.append(v)
    kept = [v for v in values if v["kept"]]
    res = {"anyCo2DataFound": resp.get("anyCo2DataFound"), "valuesReturned": len(values), "valuesKept": len(kept)}
    effective = bool(kept)  # what the pipeline would actually store

    if kind == "no_data":
        res["verdictCorrect"] = not effective
        res["hallucinated"] = [{k: v[k] for k in ("year", "field", "raw", "unit", "src")} for v in kept]
        res["anyCo2FlagCorrect"] = resp.get("anyCo2DataFound") is False
        return res

    res["verdictCorrect"] = effective
    want = str(t["year"])
    by_year = {str(y.get("year", "")).strip(): y for y in years_of(resp)}
    res["truthYearPresent"] = want in by_year
    scopes = {}
    for s in SCOPES:
        if t.get(s) is None:
            continue
        cands = [s] + (["scope2LocationBased" if s == "scope2" else "scope2"] if s in ("scope2", "scope2LocationBased") else [])
        verdict, where = "missing", None
        y = by_year.get(want, {})
        for f in cands:
            v = value_of(y.get(f))
            if v and not keep(v["src"]):
                v = None  # the pipeline would have dropped it
            j = judge_value(v, t[s])
            if j != "missing" and (verdict in ("missing", "wrong") or j.startswith("ok")):
                verdict, where = j, f
            if j.startswith("ok"):
                break
        if verdict == "ok" and where != s:
            verdict = "ok_in_" + where  # the other scope-2 field: market vs location basis is not scored
        if s == "scopes1And2" and not verdict.startswith("ok"):
            a, b = value_of(y.get("scope1")), value_of(y.get("scope2"))
            if a and b and a["tonnes"] and b["tonnes"] and close(a["tonnes"] + b["tonnes"], t[s]):
                verdict = "ok_from_parts"
        if not verdict.startswith("ok"):  # right number, wrong year label?
            for v in values:
                if v["field"] in cands and v["year"] != want and judge_value(v, t[s]).startswith("ok"):
                    verdict = "year_mismatch"
                    break
        scopes[s] = verdict
    res["scopes"] = scopes
    res["scopesOk"] = sum(1 for v in scopes.values() if v.startswith("ok"))
    res["scopesTotal"] = len(scopes)
    return res


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", required=False, default="")
    ap.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY", ""))
    ap.add_argument("--model", default="")
    ap.add_argument("--cases-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "esg-cases"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="", help="comma-separated case ids (prefix match)")
    ap.add_argument("--timeout", type=float, default=3600)
    ap.add_argument("--max-tokens", type=int, default=0, help="pipeline sends none; set only if a server needs it")
    ap.add_argument("--extra-body", default="", help="JSON merged into every request, e.g. a reasoning toggle")
    ap.add_argument("--no-json-mode", action="store_true", help="drop response_format (pipeline sends json_object)")
    ap.add_argument("--replay", default="", help="score DIR/<id>.txt answers instead of calling a server")
    ap.add_argument("--resume", action="store_true")
    a = ap.parse_args()
    if not a.replay and not (a.base_url and a.model):
        ap.error("--base-url and --model are required unless --replay is given")

    cases = [json.load(open(f)) for f in sorted(glob.glob(os.path.join(a.cases_dir, "*.json")))]
    if a.only:
        pre = [p.strip() for p in a.only.split(",") if p.strip()]
        cases = [c for c in cases if any(c["id"].startswith(p) for p in pre)]
    done = {}
    if a.resume and os.path.exists(a.out):
        done = {r["id"]: r for r in json.load(open(a.out)).get("results", [])}
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)

    results = []
    for n, c in enumerate(cases, 1):
        if c["id"] in done:
            results.append(done[c["id"]])
            continue
        if a.replay:
            p = os.path.join(a.replay, c["id"] + ".txt")
            call = {"answer": open(p).read() if os.path.exists(p) else "", "latency": 0.0}
        else:
            call = ask(a.base_url, a.api_key, a.model, c["request"], a.timeout, a.max_tokens, not a.no_json_mode,
                       json.loads(a.extra_body) if a.extra_body else None)
        r = {"id": c["id"], "kind": c["kind"], "truth": c["truth"], "latencySec": round(call.get("latency", 0), 1),
             "promptTokens": call.get("prompt_tokens"), "completionTokens": call.get("completion_tokens"),
             "finishReason": call.get("finish_reason"), "error": call.get("error"), "rawAnswer": call.get("answer", "")}
        if call.get("reasoning"):
            r["reasoning"] = call["reasoning"]
        resp, perr = parse_answer(call.get("answer", "")) if not call.get("error") else (None, "request failed")
        r["validJson"] = resp is not None
        r["parseError"] = perr
        if resp is not None:
            r.update(score(c, resp))
        else:
            r["verdictCorrect"] = False
            if c["kind"] == "positive":
                r["scopesOk"], r["scopesTotal"] = 0, sum(1 for s in SCOPES if c["truth"].get(s) is not None)
        results.append(r)
        print("[%d/%d] %-30s json=%s verdict=%s %s %.0fs" % (n, len(cases), c["id"], "y" if r["validJson"] else "n",
              "ok" if r.get("verdictCorrect") else "BAD", short(r), r["latencySec"]), file=sys.stderr, flush=True)
        save(a, results)
    save(a, results)
    table(results, a.model or "replay")


def short(r):
    if r["kind"] == "no_data":
        return "halluc=%d" % len(r.get("hallucinated") or [])
    return "scopes=%s/%s %s" % (r.get("scopesOk", 0), r.get("scopesTotal", 0),
                               " ".join("%s:%s" % (k, v) for k, v in (r.get("scopes") or {}).items() if v != "ok"))


def summary(results):
    pos = [r for r in results if r["kind"] == "positive"]
    neg = [r for r in results if r["kind"] == "no_data"]
    flat = [v for r in pos for v in (r.get("scopes") or {}).values()]
    lat = sorted(r["latencySec"] for r in results if not r.get("error"))
    return {
        "cases": len(results), "validJson": sum(r["validJson"] for r in results),
        "verdictCorrect": sum(bool(r.get("verdictCorrect")) for r in results),
        "positiveVerdictCorrect": "%d/%d" % (sum(bool(r.get("verdictCorrect")) for r in pos), len(pos)),
        "noDataVerdictCorrect": "%d/%d" % (sum(bool(r.get("verdictCorrect")) for r in neg), len(neg)),
        "scopeValuesOk": "%d/%d" % (sum(r.get("scopesOk", 0) for r in pos), sum(r.get("scopesTotal", 0) for r in pos)),
        "unitErrors": sum(1 for v in flat if v.startswith("unit_")),
        "yearMismatches": sum(1 for v in flat if v == "year_mismatch"),
        "noDataCasesWithHallucinatedValues": sum(1 for r in neg if r.get("hallucinated")),
        "medianLatencySec": lat[len(lat) // 2] if lat else None,
        "promptTokens": sum(r.get("promptTokens") or 0 for r in results),
        "completionTokens": sum(r.get("completionTokens") or 0 for r in results),
        "errors": sum(1 for r in results if r.get("error")),
    }


def save(a, results):
    meta = {"model": a.model, "baseUrl": a.base_url, "extraBody": a.extra_body or None, "replay": a.replay or None, "tolerance": TOL,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    tmp = a.out + ".tmp"
    json.dump({"meta": meta, "summary": summary(results), "results": results}, open(tmp, "w"), ensure_ascii=False, indent=1)
    os.replace(tmp, a.out)


def table(results, label):
    print("\nmodel: %s" % label)
    print("%-30s %-8s %-4s %-7s %-7s %-34s %7s %8s %6s" % ("case", "kind", "json", "verdict", "scopes", "flags", "lat(s)", "prompt", "compl"))
    for r in results:
        if r["kind"] == "no_data":
            sc, fl = "-", ("HALLUCINATED %d" % len(r["hallucinated"])) if r.get("hallucinated") else ""
        else:
            sc = "%s/%s" % (r.get("scopesOk", 0), r.get("scopesTotal", 0))
            fl = " ".join("%s:%s" % (SHORT.get(k, k), v) for k, v in (r.get("scopes") or {}).items() if v != "ok")
        if r.get("error"):
            fl = "ERROR " + r["error"][:28]
        elif not r["validJson"]:
            fl = "parse: " + (r.get("parseError") or "")[:27]
        print("%-30s %-8s %-4s %-7s %-7s %-34s %7.1f %8s %6s" % (r["id"][:30], r["kind"], "y" if r["validJson"] else "n",
              "ok" if r.get("verdictCorrect") else "BAD", sc, fl[:34], r["latencySec"], r.get("promptTokens") or "-",
              r.get("completionTokens") or "-"))
    print()
    for k, v in summary(results).items():
        print("  %-36s %s" % (k, v))


if __name__ == "__main__":
    main()
