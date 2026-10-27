#!/usr/bin/env python3
"""Run the agent_tasks.py tasks with a headless coding-agent harness against a model, inside
bubblewrap, the way Paperclip's *_local adapters run them.

  run_agent.py strata [--tasks bugfix,shell] [--max-turns 40] [--task-timeout 2700]
  run_agent.py gemma
  CLAUDE_CODE_EXTRA_BODY='{"thinking":{"type":"disabled"}}' run_agent.py strata+qwen-instruct --tag nothink
  run_agent.py claude [--claude-model sonnet]   # reference, Claude subscription

  run_agent.py --harness pi strata-nothink+qwen-instruct
  run_agent.py --harness opencode|codex|qwen|claude strata-nothink+qwen-instruct

Harnesses (--harness, default claude):
  claude    Claude Code, `claude --print --output-format stream-json` (claude_local), Anthropic API
  opencode  `opencode run --format json` (opencode_local), OpenAI chat via @ai-sdk/openai-compatible;
            $OPENCODE_BIN picks another build (2.x runs with --standalone)
  pi        `pi --mode json -p` (pi_local), OpenAI chat, provider in models.json
  codex     `codex exec --json` (codex_local), Responses API (Codex >= 0.95 has no chat wire)
  qwen      Qwen Code `qwen --output-format stream-json --yolo`, OpenAI chat
  hermes    Hermes Agent `hermes chat -q --oneshot` (hermes_local), OpenAI chat, from $HERMES_VENV
            (default /tmp/harness-eval/hermes/venv)
The non-Claude CLIs are looked up in $HARNESS_PREFIX/node_modules/.bin (default
/tmp/harness-eval/npm, an `npm install --prefix` that is bound read-only into the sandbox).
Each one gets the target's request extras the way its own config allows (thinking off,
sampling preset); what actually reached the server is in the results (thinkingOff, samplingSent).

Measurement: every task runs through capture_proxy.CaptureProxy, so turns, tokens (prompt,
cached, completion), tool calls, tool errors and repeated calls come from the wire, the same
for every harness (calls_digest.py). The proxy refuses calls past --max-calls, a uniform loop stop.

Isolation: every run gets a fresh HOME (and XDG dirs, CLAUDE_CONFIG_DIR, CODEX_HOME,
PI_CODING_AGENT_DIR, OPENCODE_CONFIG) under /tmp/strata-agent, so the user's own configs are
never read or written. bwrap mounts / read-only and only the task repo and that HOME are
writable. For the Claude reference the current OAuth access token is passed as
CLAUDE_CODE_OAUTH_TOKEN (read, never written back, never refreshed).

Raw harness output stays in /tmp/strata-agent/<run>/<task>.jsonl, the wire log in
<task>.calls.jsonl; the summary goes to results/agent-<target>.json (Claude Code) or
results/agent-<harness>-<target>.json.
"""
import argparse, json, os, shutil, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_tasks as A
import calls_digest
import targets
from capture_proxy import CaptureProxy

CLAUDE = os.path.realpath(os.path.expanduser("~/.local/bin/claude"))
PREFIX = os.environ.get("HARNESS_PREFIX", "/tmp/harness-eval/npm")
BIN = os.path.join(PREFIX, "node_modules", ".bin")
OPENCODE = os.environ.get("OPENCODE_BIN", os.path.join(BIN, "opencode"))  # e.g. the nixpkgs 2.x build
HERMES_VENV = os.environ.get("HERMES_VENV", "/tmp/harness-eval/hermes/venv")
CONTEXT, MAX_OUT = 262144, 32768


def base_env(home):
    return {"HOME": home, "PATH": BIN + ":" + os.environ["PATH"], "LANG": "C.UTF-8", "TERM": "dumb",
            "XDG_CONFIG_HOME": home + "/.config", "XDG_DATA_HOME": home + "/.local/share",
            "XDG_CACHE_HOME": home + "/.cache", "XDG_STATE_HOME": home + "/.local/state"}


def env_for(target, model, base_home, root=None):
    """Claude Code: Anthropic endpoint and extra body, or the subscription for 'claude'."""
    e = dict(base_env(base_home), CLAUDE_CONFIG_DIR=os.path.join(base_home, ".claude"),
             CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_TELEMETRY="1", DISABLE_ERROR_REPORTING="1",
             DISABLE_AUTOUPDATER="1", DISABLE_BUG_COMMAND="1", CLAUDE_CODE_DISABLE_TERMINAL_TITLE="1",
             API_TIMEOUT_MS=os.environ.get("API_TIMEOUT_MS", "3600000"), BASH_DEFAULT_TIMEOUT_MS="120000")
    if target == "claude":
        creds = json.load(open(os.path.expanduser("~/.claude/.credentials.json")))["claudeAiOauth"]
        if creds["expiresAt"] / 1000 - time.time() < 1800:
            raise SystemExit("Claude access token expires in <30 min; open a normal claude session to refresh it first")
        e["CLAUDE_CODE_OAUTH_TOKEN"] = creds["accessToken"]
        return e, model
    t = targets.get(target)
    e.update({"ANTHROPIC_BASE_URL": root or t["root"], "ANTHROPIC_API_KEY": t["key"] or "dummy",
              "ANTHROPIC_MODEL": t["model"], "ANTHROPIC_DEFAULT_OPUS_MODEL": t["model"],
              "ANTHROPIC_DEFAULT_SONNET_MODEL": t["model"], "ANTHROPIC_DEFAULT_HAIKU_MODEL": t["model"],
              "ANTHROPIC_SMALL_FAST_MODEL": t["model"]})
    for k in ("DISABLE_PROMPT_CACHING", "CLAUDE_CODE_MAX_OUTPUT_TOKENS", "MAX_THINKING_TOKENS", "CLAUDE_CODE_EXTRA_BODY",
              "CLAUDE_CODE_ATTRIBUTION_HEADER"):
        if os.environ.get(k):
            e[k] = os.environ[k]
    if t["extra_anthropic"]:
        # Claude Code 2.1.x sends no temperature/top_p/top_k of its own (checked with a capture proxy),
        # so Strata would decode greedy; CLAUDE_CODE_EXTRA_BODY is merged into every /v1/messages body.
        e["CLAUDE_CODE_EXTRA_BODY"] = json.dumps(dict(json.loads(e.get("CLAUDE_CODE_EXTRA_BODY") or "{}"), **t["extra_anthropic"]))
    return e, t["model"]


def wjson(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(obj, open(path, "w"), indent=1)


def h_claude(t, root, home, prompt, a):
    env, model = env_for(a.target, a.claude_model, home, root)
    return [CLAUDE, "--print", "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions",
            "--max-turns", str(a.max_turns), "--model", model, prompt], env


def h_opencode(t, root, home, prompt, a):
    # temperature/top_p are AI SDK call settings (agent config, honoured by 1.x); the model options
    # are merged into the request body by @ai-sdk/openai-compatible (2.x only sends these).
    extra = dict(t["extra"])
    call = {k: extra[k] for k in ("temperature", "top_p") if k in extra}
    cfg = {"$schema": "https://opencode.ai/config.json", "autoupdate": False, "share": "disabled",
           "model": "strata/" + t["model"], "small_model": "strata/" + t["model"],
           "permission": {"edit": "allow", "bash": "allow", "webfetch": "deny", "external_directory": "deny"},
           "agent": {"build": call},
           "provider": {"strata": {"npm": "@ai-sdk/openai-compatible", "name": "Strata",
                                   "options": {"baseURL": root + "/v1", "apiKey": t["key"] or "none"},
                                   "models": {t["model"]: {"name": t["model"], "tool_call": True, "reasoning": False, "temperature": True,
                                                           "limit": {"context": CONTEXT, "output": MAX_OUT}, "options": extra}}}}}
    path = os.path.join(home, ".config", "opencode", "opencode.json")
    wjson(path, cfg)
    env = dict(base_env(home), OPENCODE_CONFIG=path, OPENCODE_DISABLE_AUTOUPDATE="1", OPENCODE_DISABLE_MODELS_FETCH="1",
               OPENCODE_DISABLE_LSP_DOWNLOAD="1", OPENCODE_DISABLE_SHARE="1")
    # opencode 2.x talks to a shared background service unless told --standalone.
    v2 = subprocess.run([OPENCODE, "--version"], capture_output=True, text=True, env=env).stdout.strip().lstrip("v").startswith("2.")
    return [OPENCODE, "run", "--format", "json", "--auto"] + (["--standalone"] if v2 else []) + [
        "--model", "strata/" + t["model"], prompt], env


def h_pi(t, root, home, prompt, a):
    # thinkingFormat qwen-chat-template turns `--thinking off` into chat_template_kwargs.enable_thinking=false;
    # samplingParams is merged into every body. The tool list is the one Paperclip's pi_local passes.
    extra = {k: v for k, v in t["extra"].items() if k != "chat_template_kwargs"}
    think = (t["extra"].get("chat_template_kwargs") or {}).get("enable_thinking", True)
    agent_dir = os.path.join(home, ".pi", "agent")
    wjson(os.path.join(agent_dir, "models.json"), {"providers": {"strata": {
        "baseUrl": root + "/v1", "api": "openai-completions", "apiKey": t["key"] or "none",
        "compat": {"thinkingFormat": "qwen-chat-template", "supportsDeveloperRole": False, "supportsStore": False,
                   "supportsReasoningEffort": False, "maxTokensField": "max_tokens"},
        "models": [{"id": t["model"], "reasoning": True, "contextWindow": CONTEXT, "maxTokens": MAX_OUT,
                    "input": ["text"], "samplingParams": extra}]}}})
    env = dict(base_env(home), PI_CODING_AGENT_DIR=agent_dir, PI_OFFLINE="1", PI_TELEMETRY="0")
    return [os.path.join(BIN, "pi"), "--mode", "json", "-p", "--no-session", "--provider", "strata", "--model", t["model"],
            "--thinking", "medium" if think else "off", "--tools", "read,bash,edit,write,grep,find,ls", prompt], env


def h_codex(t, root, home, prompt, a):
    # Responses API only. Codex has no setting for sampling fields; reasoning effort "none" is
    # sent as reasoning.effort, which Strata maps to thinking off.
    think = (t["extra"].get("chat_template_kwargs") or {}).get("enable_thinking", True)
    cx = os.path.join(home, ".codex")
    os.makedirs(cx, exist_ok=True)
    open(os.path.join(cx, "config.toml"), "w").write(
        'model = "%s"\nmodel_provider = "strata"\nmodel_reasoning_effort = "%s"\napproval_policy = "never"\n'
        'sandbox_mode = "danger-full-access"\nmodel_context_window = %d\n[analytics]\nenabled = false\n'
        '[model_providers.strata]\nname = "Strata"\nbase_url = "%s/v1"\nwire_api = "responses"\nenv_key = "STRATA_KEY"\n'
        % (t["model"], "medium" if think else "none", CONTEXT, root))
    env = dict(base_env(home), CODEX_HOME=cx, STRATA_KEY=t["key"] or "none")
    return [os.path.join(BIN, "codex"), "exec", "--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox",
            "-m", t["model"], prompt], env


def h_qwen(t, root, home, prompt, a):
    extra = dict(t["extra"])
    ctk = extra.pop("chat_template_kwargs", None)
    gen = {"samplingParams": extra, "contextWindowSize": CONTEXT}
    if ctk:
        gen["extra_body"] = {"chat_template_kwargs": ctk}
    wjson(os.path.join(home, ".qwen", "settings.json"), {
        "security": {"auth": {"selectedType": "openai"}}, "model": {"name": t["model"], "generationConfig": gen},
        "privacy": {"usageStatisticsEnabled": False}, "telemetry": {"enabled": False}, "general": {"disableAutoUpdate": True}})
    env = dict(base_env(home), OPENAI_BASE_URL=root + "/v1", OPENAI_API_KEY=t["key"] or "none", OPENAI_MODEL=t["model"])
    return [os.path.join(BIN, "qwen"), "--output-format", "stream-json", "--yolo", "--auth-type", "openai", "-m", t["model"],
            "--max-session-turns", str(a.max_turns * 2), prompt], env


def h_hermes(t, root, home, prompt, a):
    # A named custom provider; extra_body is merged into every chat-completions request.
    think = (t["extra"].get("chat_template_kwargs") or {}).get("enable_thinking", True)
    hh = os.path.join(home, ".hermes")
    wjson(os.path.join(hh, "config.yaml"), {  # JSON is valid YAML
        "model": {"provider": "strata", "default": t["model"], "context_length": CONTEXT},
        "providers": {"strata": {"api": root + "/v1", "default_model": t["model"], "api_key": t["key"] or "none",
                                 "extra_body": t["extra"]}},
        "agent": {"reasoning_effort": "medium" if think else "none"}})
    env = dict(base_env(home), HERMES_HOME=hh)
    return [os.path.join(HERMES_VENV, "bin", "hermes"), "chat", "-q", prompt, "--oneshot", "-Q", "--provider", "strata",
            "-m", t["model"], "--yolo", "--max-turns", str(a.max_turns * 2)], env


HARNESSES = {"claude": h_claude, "opencode": h_opencode, "pi": h_pi, "codex": h_codex, "qwen": h_qwen, "hermes": h_hermes}


def bwrap(cmd, work, home):
    ro = [x for d in (PREFIX, os.path.dirname(HERMES_VENV)) if os.path.isdir(d) for x in ("--ro-bind", d, d)]
    return ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"] + ro + [
        "--bind", work, work, "--bind", home, home, "--chdir", work, "--unshare-pid", "--die-with-parent", "--"] + cmd


def digest(jsonl):
    """Claude Code only: turns, tool calls/errors, API errors and the final result from stream-json."""
    s = {"toolCalls": 0, "toolErrors": 0, "apiErrors": 0, "assistantMsgs": 0, "result": None, "toolErrorSamples": []}
    for line in open(jsonl, errors="replace"):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        ty = ev.get("type")
        if ty == "assistant":
            s["assistantMsgs"] += 1
            s["toolCalls"] += sum(1 for b in ev.get("message", {}).get("content", []) if b.get("type") == "tool_use")
            if ev.get("error") or ev.get("isApiErrorMessage"):
                s["apiErrors"] += 1
        elif ty == "user":
            for b in ev.get("message", {}).get("content", []) if isinstance(ev.get("message", {}).get("content"), list) else []:
                if b.get("type") == "tool_result" and b.get("is_error"):
                    s["toolErrors"] += 1
                    c = b.get("content")
                    txt = c if isinstance(c, str) else json.dumps(c)
                    if len(s["toolErrorSamples"]) < 4:
                        s["toolErrorSamples"].append(txt[:160])
        elif ty == "system" and "error" in str(ev.get("subtype", "")) or ty == "system" and ev.get("subtype") == "api_retry":
            s["apiErrors"] += 1
        elif ty == "result":
            s["result"] = {k: ev.get(k) for k in ("subtype", "is_error", "num_turns", "duration_ms", "duration_api_ms", "total_cost_usd")}
            s["result"]["text"] = (ev.get("result") or "")[:300]
            s["usage"] = ev.get("usage")
    return s


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="a targets.py name, or 'claude' for the subscription reference")
    ap.add_argument("--harness", default="claude", choices=sorted(HARNESSES))
    ap.add_argument("--tasks", default=",".join(A.TASKS))
    ap.add_argument("--max-turns", type=int, default=40, help="Claude Code --max-turns (Qwen Code gets twice this)")
    ap.add_argument("--max-calls", type=int, default=80, help="model calls the capture proxy lets through per task")
    ap.add_argument("--task-timeout", type=int, default=2700)
    ap.add_argument("--claude-model", default="sonnet")
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    if a.harness != "claude" and a.target == "claude":
        raise SystemExit("the subscription reference only runs with --harness claude")
    name = ("" if a.harness == "claude" else a.harness + "-") + a.target + (("-" + a.tag) if a.tag else "")
    root = os.path.join("/tmp/strata-agent", "%s-%s" % (name, time.strftime("%Y%m%d-%H%M%S")))
    os.makedirs(root)
    t = None if a.target == "claude" else targets.get(a.target)
    out = []
    for task in a.tasks.split(","):
        work, home = os.path.join(root, task), os.path.join(root, "home-" + task)
        os.makedirs(home)
        hashes = A.setup(task, work)
        calls_log = os.path.join(root, task + ".calls.jsonl")
        proxy = CaptureProxy(t["root"], calls_log, a.max_calls).start() if t else None
        cmd, env = HARNESSES[a.harness](t, "http://127.0.0.1:%d" % proxy.port if proxy else None, home, A.PROMPTS[task], a)
        log = os.path.join(root, task + ".jsonl")
        t0 = time.time()
        timed_out = False
        with open(log, "w") as f, open(os.path.join(root, task + ".stderr"), "w") as ferr:
            try:
                p = subprocess.run(bwrap(cmd, work, home), env=env, stdout=f, stderr=ferr, stdin=subprocess.DEVNULL,
                                   timeout=a.task_timeout)
                rc = p.returncode
            except subprocess.TimeoutExpired:
                rc, timed_out = None, True
        wall = time.time() - t0
        if proxy:
            proxy.stop()
        ok, detail = A.check(task, work, hashes)
        r = {"task": task, "pass": ok, "check": detail, "wallSec": round(wall), "rc": rc, "timedOut": timed_out, "log": log}
        if proxy and os.path.exists(calls_log):
            r["wire"] = calls_digest.digest(calls_log)
        if a.harness == "claude":
            d = digest(log)
            r.update({"turns": (d["result"] or {}).get("num_turns"), "toolCalls": d["toolCalls"], "toolErrors": d["toolErrors"],
                      "apiErrors": d["apiErrors"], "resultSubtype": (d["result"] or {}).get("subtype"),
                      "isError": (d["result"] or {}).get("is_error"), "finalText": (d["result"] or {}).get("text"),
                      "toolErrorSamples": d["toolErrorSamples"], "usage": d.get("usage")})
        if rc != 0 or timed_out:
            r["stderrTail"] = open(os.path.join(root, task + ".stderr"), errors="replace").read()[-600:]
        out.append(r)
        w = r.get("wire") or {}
        print("%-9s %s calls=%s tools=%s toolErr=%s fmtErr=%s rep=%s http=%s in=%s cached=%s out=%s %4ds %s | %s" % (
            task, "PASS" if ok else "FAIL", w.get("calls", r.get("turns")), w.get("toolCalls", r.get("toolCalls")),
            w.get("toolErrors", r.get("toolErrors")), w.get("formatErrors"), w.get("maxRepeat"), w.get("httpErrors"),
            w.get("promptTok"), w.get("cachedTok"), w.get("outTok"), wall,
            "TIMEOUT" if timed_out else "rc=%s" % rc, detail[:100]), flush=True)
        shutil.rmtree(home, ignore_errors=True)  # holds the OAuth token for the claude target
    summ = {"harness": a.harness, "target": a.target, "model": t["model"] if t else a.claude_model,
            "extra": (t or {}).get("extra"), "extraAnthropic": (t or {}).get("extra_anthropic"),
            "pass": sum(r["pass"] for r in out), "total": len(out), "wallSec": sum(r["wallSec"] for r in out), "run": root}
    for k in ("calls", "promptTok", "cachedTok", "outTok", "toolCalls", "toolErrors", "formatErrors", "httpErrors"):
        summ[k] = sum((r.get("wire") or {}).get(k) or 0 for r in out)
    print("SUMMARY", json.dumps(summ))
    res = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
    os.makedirs(res, exist_ok=True)
    json.dump({"summary": summ, "results": out}, open(os.path.join(res, "agent-%s.json" % name), "w"), indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
