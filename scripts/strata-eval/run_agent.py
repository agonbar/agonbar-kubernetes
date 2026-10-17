#!/usr/bin/env python3
"""Run the agent_tasks.py tasks with headless Claude Code (the way Paperclip's claude_local
does: `claude --print --output-format stream-json`) against a model, inside bubblewrap.

  run_agent.py strata [--tasks bugfix,shell] [--max-turns 40] [--task-timeout 2700]
  run_agent.py gemma
  CLAUDE_CODE_EXTRA_BODY='{"thinking":{"type":"disabled"}}' run_agent.py strata --tag nothink
  run_agent.py claude [--claude-model sonnet]   # reference, Claude subscription

Isolation: every run gets a fresh HOME/CLAUDE_CONFIG_DIR under /tmp/strata-agent, so the
user's ~/.claude (settings, hooks, MCP servers, CLAUDE.md, login) is never read or written.
bwrap mounts / read-only and only the task repo and that HOME are writable. For the Claude
reference the current OAuth access token is passed as CLAUDE_CODE_OAUTH_TOKEN (read, never
written back, never refreshed), so the user's session file is untouched.

Per task: success (scripted check), turns, wall time, tool errors, API errors.
Raw stream-json stays in /tmp/strata-agent/<run>/<task>.jsonl; the summary goes to
results/agent-<target>.json.
"""
import argparse, json, os, shutil, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_tasks as A
import targets

CLAUDE = os.path.realpath(os.path.expanduser("~/.local/bin/claude"))


def env_for(target, model, base_home):
    e = {"HOME": base_home, "CLAUDE_CONFIG_DIR": os.path.join(base_home, ".claude"), "PATH": os.environ["PATH"],
         "LANG": "C.UTF-8", "TERM": "dumb",
         "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1",
         "DISABLE_AUTOUPDATER": "1", "DISABLE_BUG_COMMAND": "1", "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1",
         "API_TIMEOUT_MS": os.environ.get("API_TIMEOUT_MS", "3600000"), "BASH_DEFAULT_TIMEOUT_MS": "120000"}
    if target == "claude":
        creds = json.load(open(os.path.expanduser("~/.claude/.credentials.json")))["claudeAiOauth"]
        if creds["expiresAt"] / 1000 - time.time() < 1800:
            raise SystemExit("Claude access token expires in <30 min; open a normal claude session to refresh it first")
        e["CLAUDE_CODE_OAUTH_TOKEN"] = creds["accessToken"]
        return e, model
    t = targets.get(target)
    e.update({"ANTHROPIC_BASE_URL": t["root"], "ANTHROPIC_API_KEY": t["key"] or "dummy",
              "ANTHROPIC_MODEL": t["model"], "ANTHROPIC_DEFAULT_OPUS_MODEL": t["model"],
              "ANTHROPIC_DEFAULT_SONNET_MODEL": t["model"], "ANTHROPIC_DEFAULT_HAIKU_MODEL": t["model"],
              "ANTHROPIC_SMALL_FAST_MODEL": t["model"]})
    for k in ("DISABLE_PROMPT_CACHING", "CLAUDE_CODE_MAX_OUTPUT_TOKENS", "MAX_THINKING_TOKENS", "CLAUDE_CODE_EXTRA_BODY"):
        if os.environ.get(k):
            e[k] = os.environ[k]
    return e, t["model"]


def bwrap(cmd, work, home):
    return ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
            "--bind", work, work, "--bind", home, home, "--chdir", work, "--unshare-pid", "--die-with-parent", "--"] + cmd


def digest(jsonl):
    """Turns, tool calls/errors, API errors and the final result from a stream-json log."""
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
    ap.add_argument("--tasks", default=",".join(A.TASKS))
    ap.add_argument("--max-turns", type=int, default=40)
    ap.add_argument("--task-timeout", type=int, default=2700)
    ap.add_argument("--claude-model", default="sonnet")
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    run = "%s%s-%s" % (a.target, ("-" + a.tag) if a.tag else "", time.strftime("%Y%m%d-%H%M%S"))
    root = os.path.join("/tmp/strata-agent", run)
    os.makedirs(root)
    out = []
    for task in a.tasks.split(","):
        work, home = os.path.join(root, task), os.path.join(root, "home-" + task)
        os.makedirs(home)
        hashes = A.setup(task, work)
        env, model = env_for(a.target, a.claude_model, home)
        cmd = [CLAUDE, "--print", "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions",
               "--max-turns", str(a.max_turns), "--model", model, A.PROMPTS[task]]
        log = os.path.join(root, task + ".jsonl")
        t0 = time.time()
        timed_out = False
        with open(log, "w") as f, open(os.path.join(root, task + ".stderr"), "w") as ferr:
            try:
                p = subprocess.run(bwrap(cmd, work, home), env=env, stdout=f, stderr=ferr, timeout=a.task_timeout)
                rc = p.returncode
            except subprocess.TimeoutExpired:
                rc, timed_out = None, True
        wall = time.time() - t0
        ok, detail = A.check(task, work, hashes)
        d = digest(log)
        r = {"task": task, "pass": ok, "check": detail, "wallSec": round(wall), "rc": rc, "timedOut": timed_out,
             "turns": (d["result"] or {}).get("num_turns"), "toolCalls": d["toolCalls"], "toolErrors": d["toolErrors"],
             "apiErrors": d["apiErrors"], "resultSubtype": (d["result"] or {}).get("subtype"),
             "isError": (d["result"] or {}).get("is_error"), "finalText": (d["result"] or {}).get("text"),
             "toolErrorSamples": d["toolErrorSamples"], "usage": d.get("usage"), "log": log}
        if not d["result"]:
            r["stderrTail"] = open(os.path.join(root, task + ".stderr"), errors="replace").read()[-600:]
        out.append(r)
        print("%-9s %s turns=%s tools=%d toolErr=%d apiErr=%d %4ds %s | %s" % (task, "PASS" if ok else "FAIL", r["turns"], r["toolCalls"],
              r["toolErrors"], r["apiErrors"], wall, r["resultSubtype"] or ("TIMEOUT" if timed_out else "rc=%s" % rc), detail[:120]), flush=True)
        shutil.rmtree(home, ignore_errors=True)  # holds the OAuth token for the claude target
    summ = {"target": a.target, "model": model, "pass": sum(r["pass"] for r in out), "total": len(out),
            "wallSec": sum(r["wallSec"] for r in out), "run": root}
    print("SUMMARY", json.dumps(summ))
    res = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
    os.makedirs(res, exist_ok=True)
    json.dump({"summary": summ, "results": out}, open(os.path.join(res, "agent-%s%s.json" % (a.target, ("-" + a.tag) if a.tag else "")), "w"),
              indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
