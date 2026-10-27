"""Harness-independent metrics from a capture_proxy log (one JSON line per model call).

  digest(path) -> dict   turns, tokens (prompt, cached, completion), first-call prompt size,
                         tools offered, what the harness sent for thinking and sampling,
                         tool calls and results rebuilt from the request histories, tool
                         errors, repeated identical calls, HTTP errors.

Tool calls and their results are read back from the conversation each harness resends:
OpenAI chat (assistant tool_calls + role tool), Anthropic (tool_use / tool_result) and
Responses (function_call / custom_tool_call and their *_output), keyed by call id.
A result counts as an error when the protocol flags it (Anthropic is_error) or its text
starts like one; the samples in the output show what that caught.
"""
import json, re

ERR = re.compile(r"^\W{0,3}(error|failed|failure|invalid|exception|traceback|validation|unknown tool|tool .* not found|"
                 r"no such file|could not|cannot|file not found|old_?string|the edit|aborted|denied|"
                 r"exit code: ?[1-9]|process exited with code [1-9]|.{0,40}\b(failed|rejected)\b)", re.I)
FORMAT = re.compile(r"(validat|invalid (json|argument|input|param)|expected (a |an )?(boolean|string|number|object|array)|"
                    r"must be (a |an )?(boolean|string|number|object)|required (property|parameter)|parse|schema|"
                    r"unknown tool|not a valid)", re.I)
SAMPLING_KEYS = ("temperature", "top_p", "top_k", "min_p", "presence_penalty", "repetition_penalty")


def _prompt_tokens(u):
    if "prompt_tokens" in u:
        return u["prompt_tokens"], (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0, u.get("completion_tokens") or 0
    if "cache_read_input_tokens" in u or (u.get("input_tokens") is not None and "input_tokens_details" not in u):
        cr = u.get("cache_read_input_tokens") or 0
        return (u.get("input_tokens") or 0) + cr + (u.get("cache_creation_input_tokens") or 0), cr, u.get("output_tokens") or 0
    if "input_tokens" in u:
        return u["input_tokens"], (u.get("input_tokens_details") or {}).get("cached_tokens") or 0, u.get("output_tokens") or 0
    return None, None, None


def _text(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(_text(x.get("text") if isinstance(x, dict) and "text" in x else x.get("content") if isinstance(x, dict) else x) for x in c)
    return "" if c is None else json.dumps(c)


def _history(req, calls, results, order):
    """Collect tool calls (id -> (name, args)) and results (id -> (text, flagged)) of one request."""
    for m in req.get("messages") or []:
        for tc in m.get("tool_calls") or []:
            f = tc.get("function") or {}
            if tc.get("id") not in calls:
                order.append(tc.get("id"))
            calls[tc.get("id")] = (f.get("name"), f.get("arguments"))
        if m.get("role") == "tool":
            results[m.get("tool_call_id")] = (_text(m.get("content")), False)
        if isinstance(m.get("content"), list):
            for b in m["content"]:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    if b.get("id") not in calls:
                        order.append(b.get("id"))
                    calls[b.get("id")] = (b.get("name"), json.dumps(b.get("input"), sort_keys=True))
                elif b.get("type") == "tool_result":
                    results[b.get("tool_use_id")] = (_text(b.get("content")), bool(b.get("is_error")))
    for i in req.get("input") if isinstance(req.get("input"), list) else []:
        ty = i.get("type")
        if ty in ("function_call", "custom_tool_call", "local_shell_call"):
            if i.get("call_id") not in calls:
                order.append(i.get("call_id"))
            calls[i.get("call_id")] = (i.get("name") or ty, i.get("arguments") or i.get("input") or json.dumps(i.get("action")))
        elif ty in ("function_call_output", "custom_tool_call_output", "local_shell_call_output"):
            results[i.get("call_id")] = (_text(i.get("output")), False)


def _thinking_off(req):
    if (req.get("chat_template_kwargs") or {}).get("enable_thinking") is False:
        return True
    if req.get("reasoning_effort") == "none" or (req.get("reasoning") or {}).get("effort") in ("none",):
        return True
    if (req.get("thinking") or {}).get("type") == "disabled":
        return True
    return False


def digest(path):
    recs = [json.loads(l) for l in open(path) if l.strip()]
    model = [r for r in recs if r.get("req") is not None]
    main = [r for r in model if r["req"].get("tools")]
    d = {"calls": len(model), "toolCallsTurns": len(main), "refused": sum(1 for r in recs if r.get("refused")),
         "httpErrors": sum(1 for r in model if r.get("status") != 200),
         "httpErrorSamples": [(r.get("status"), (r.get("errorBody") or r.get("proxyError") or "")[:200]) for r in model if r.get("status") != 200][:3],
         "promptTok": 0, "cachedTok": 0, "outTok": 0, "modelSec": round(sum(r.get("sec") or 0 for r in model))}
    for r in model:
        p, c, o = _prompt_tokens(r.get("usage") or {})
        d["promptTok"] += p or 0
        d["cachedTok"] += c or 0
        d["outTok"] += o or 0
    if main:
        first = main[0]
        d["firstPromptTok"] = _prompt_tokens(first.get("usage") or {})[0]
        d["firstCallSec"] = first.get("sec")
        req = first["req"]
        d["toolsOffered"] = len(req["tools"])
        d["toolsChars"] = len(json.dumps(req["tools"]))
        sysm = req.get("system") or req.get("instructions") or [m.get("content") for m in req.get("messages") or [] if m.get("role") in ("system", "developer")]
        d["systemChars"] = len(_text(sysm))
        d["api"] = first["path"].rsplit("/", 1)[-1]
        d["thinkingOff"] = _thinking_off(req)
        d["samplingSent"] = {k: req[k] for k in SAMPLING_KEYS if k in req}
        d["streamed"] = bool(req.get("stream"))
    calls, results, order = {}, {}, []
    for r in model:
        _history(r["req"], calls, results, order)
    seq = [calls[i] for i in order]
    d["toolCalls"] = len(seq)
    errs = [(calls.get(i, ("?",))[0], t) for i, (t, flagged) in results.items() if flagged or ERR.match(t.strip())]
    d["toolErrors"] = len(errs)
    d["formatErrors"] = sum(1 for _, t in errs if FORMAT.search(t[:300]))
    d["toolErrorSamples"] = ["%s: %s" % (n, t.strip()[:150].replace("\n", " ")) for n, t in errs[:5]]
    run = best = 1
    for a, b in zip(seq, seq[1:]):
        run = run + 1 if a == b else 1
        best = max(best, run)
    d["maxRepeat"] = best if seq else 0
    d["dupCalls"] = len(seq) - len(set(map(tuple, seq)))
    d["toolMix"] = {}
    for n, _ in seq:
        d["toolMix"][n] = d["toolMix"].get(n, 0) + 1
    return d


if __name__ == "__main__":
    import sys
    print(json.dumps(digest(sys.argv[1]), indent=1, ensure_ascii=False))
