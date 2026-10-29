#!/usr/bin/env python3
"""Run the real-commit tasks (tasks.py) with one contender and verify each result (verify.py).

  run.py selftest [--tasks a,b]          every verifier fails on commit^ and passes on the commit
  run.py reverify --of CONTENDER         rerun the verifiers on kept workspaces, rewrite pass/check
  run.py strata-nothink [--tasks a,b]    pi + Strata, thinking off, instruct sampling (pi.nix)
  run.py strata-think   [--tasks a,b]    pi + Strata, thinking on, Qwen think sampling
  run.py strata-harness [--tasks a,b]    strata-nothink plus harness/junior.ts (plan, budget, finish)
  run.py gemma          [--tasks a,b]    pi + gemma-4-12B on work-vm-00's llama-server
  run.py claude-sonnet  [--tasks a,b]    Claude Code + Sonnet on the subscription (reference)

pi gets the user's own config (~/.pi/agent: models.json, settings.json, extensions/web.ts with
web_search and web_fetch) with the base URL pointed at a capture_proxy in front of the model,
which counts calls and tokens on the wire and refuses calls past --max-calls. Claude Code talks
to Anthropic directly; its numbers come from its stream-json result. Each task runs in
sandbox.py's bubblewrap with a fresh HOME, under a wall-clock cap (--timeout).

Strata serves one request at a time: before every Strata task this waits until its /status
reports idle (another client may be using it), and a lock file keeps two Strata contenders of
this harness from overlapping. Claude and gemma runs may go in parallel with a Strata run.

Raw logs, workspaces and diffs stay in /tmp/realtasks/runs/<contender>/<task>/; one JSON line
per task goes to results/<contender>.jsonl (metrics and verifier detail only, no repo content).
"""
import argparse, fcntl, json, os, shutil, subprocess, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import tasks as T
import sandbox as S
import verify as V
import calls_digest
import targets
from capture_proxy import CaptureProxy

ROOT = "/tmp/realtasks"
SRC = ROOT + "/src"
PI_DIR = os.path.expanduser("~/.pi/agent")
PI_TOOLS = "read,bash,edit,write,grep,find,ls,web_search,web_fetch"
JUNIOR_TS = os.path.join(HERE, "harness", "junior.ts")
JUNIOR_CALLS = 100  # the harness's own budget; the proxy cap (--max-calls) sits above it
CLAUDE = os.path.realpath(os.path.expanduser("~/.local/bin/claude"))
CARGO_SEED = ROOT + "/cargo-home"  # crates.io index and sources, fetched once, copied per run


def git(*a, cwd=None, check=True):
    return subprocess.run(["git"] + list(a), cwd=cwd, capture_output=True, text=True, check=check).stdout


def source(repo):
    d = os.path.join(SRC, repo)
    url = os.path.expanduser(T.REPOS[repo])
    if not os.path.isdir(d):
        os.makedirs(SRC, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--no-local", url, d], check=True)
    return d


def prepare(task, dest, real=False):
    """dest = the repo at commit^ (or the commit), with no history past it and no remote."""
    t = T.TASKS[task]
    src = source(t["repo"])
    sha = git("rev-parse", t["commit"] + ("" if real else "^"), cwd=src).strip()
    br = "rt-" + task
    git("branch", "-f", br, sha, cwd=src)
    if os.path.exists(dest):
        shutil.rmtree(dest)
    subprocess.run(["git", "clone", "-q", "--no-local", "--single-branch", "--no-tags", "-b", br, "file://" + src, dest], check=True)
    git("branch", "-m", br, "main", cwd=dest)
    git("remote", "remove", "origin", cwd=dest)
    if t.get("nix"):  # private flake inputs are fetched here, with the user's SSH agent, not in the sandbox.
        # Not fatal: an old lock can pin a local path input whose content moved on, and eval is lazy
        # enough not to need it.
        subprocess.run(["nix", "flake", "archive", "--no-write-lock-file", "--json", "./nix"], cwd=dest,
                       capture_output=True)
    return sha


def new_home(path, task):
    os.makedirs(path, exist_ok=True)
    if T.TASKS[task]["repo"] == "benilla" and os.path.isdir(CARGO_SEED):
        subprocess.run(["cp", "-a", CARGO_SEED, os.path.join(path, ".cargo")], check=True)
    return path


# ------------------------------------------------------------------ contenders

def strata_root():
    url = json.load(open(os.path.join(PI_DIR, "models.json")))["providers"]["strata"]["baseUrl"]
    return url[:-3] if url.endswith("/v1") else url


def pi_agent_dir(home, contender, proxy_url):
    ad = os.path.join(home, ".pi", "agent")
    os.makedirs(os.path.join(ad, "extensions"), exist_ok=True)
    shutil.copy(os.path.join(PI_DIR, "settings.json"), ad)
    shutil.copy(os.path.join(PI_DIR, "extensions", "web.ts"), os.path.join(ad, "extensions"))
    if contender == "strata-harness":
        shutil.copy(JUNIOR_TS, os.path.join(ad, "extensions"))
    models = json.load(open(os.path.join(PI_DIR, "models.json")))
    if contender.startswith("strata"):
        prov = models["providers"]["strata"]
        prov["baseUrl"] = proxy_url + "/v1"
        if contender == "strata-think":  # the model card's thinking preset
            prov["models"][0]["samplingParams"] = dict(targets.SAMPLING["qwen-think"])
        provider, model = "strata", prov["models"][0]["id"]
    else:  # gemma
        g = targets.get("gemma")
        models = {"providers": {"gemma": {
            "baseUrl": proxy_url + "/v1", "api": "openai-completions", "apiKey": g["key"] or "none",
            "compat": {"supportsDeveloperRole": False, "supportsStore": False, "supportsReasoningEffort": False,
                       "maxTokensField": "max_tokens"},
            "models": [{"id": g["model"], "reasoning": False, "input": ["text"], "contextWindow": 131072, "maxTokens": 16384}]}}}
        provider, model = "gemma", g["model"]
    json.dump(models, open(os.path.join(ad, "models.json"), "w"), indent=1)
    return ad, provider, model


def upstream(contender):
    return strata_root() if contender.startswith("strata") else targets.get("gemma")["root"]


def strata_status():
    try:
        return json.load(urllib.request.urlopen(strata_root() + "/status", timeout=10))
    except Exception as e:
        return {"error": repr(e)}


def wait_strata_idle(log):
    """Three idle readings 10 s apart; returns the seconds spent waiting. REALTASKS_NO_WAIT=1 skips it
    (Strata queues requests; wall times are then not comparable)."""
    if os.environ.get("REALTASKS_NO_WAIT") == "1":
        return 0
    t0, ok = time.time(), 0
    while ok < 3:
        s = strata_status()
        ok = ok + 1 if (not s.get("busy") and not s.get("queued") and "error" not in s) else 0
        if not ok:
            log("strata busy (%s), waiting" % {k: s.get(k) for k in ("busy", "queued", "phase", "error")})
            time.sleep(30)
        else:
            time.sleep(10)
    return round(time.time() - t0)


def claude_env(home, log=print):
    """The current OAuth access token, read only. It must outlive a task (40 min); the user's own
    Claude sessions refresh it, so wait for that rather than refreshing it here."""
    for _ in range(90):
        creds = json.load(open(os.path.expanduser("~/.claude/.credentials.json")))["claudeAiOauth"]
        if creds["expiresAt"] / 1000 - time.time() >= 2400:
            break
        log("Claude token expires in <40 min, waiting for a refresh")
        time.sleep(60)
    else:
        raise SystemExit("Claude access token was not refreshed; open a normal claude session")
    return {"CLAUDE_CONFIG_DIR": home + "/.claude", "CLAUDE_CODE_OAUTH_TOKEN": creds["accessToken"],
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1",
            "DISABLE_AUTOUPDATER": "1", "DISABLE_BUG_COMMAND": "1", "BASH_DEFAULT_TIMEOUT_MS": "600000",
            "BASH_MAX_TIMEOUT_MS": "1200000"}


def claude_digest(jsonl):
    s = {"calls": None, "toolCalls": 0, "toolErrors": 0}
    for line in open(jsonl, errors="replace"):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "assistant":
            s["toolCalls"] += sum(1 for b in ev.get("message", {}).get("content", []) if b.get("type") == "tool_use")
        elif ev.get("type") == "user" and isinstance(ev.get("message", {}).get("content"), list):
            s["toolErrors"] += sum(1 for b in ev["message"]["content"] if b.get("type") == "tool_result" and b.get("is_error"))
        elif ev.get("type") == "result":
            u = ev.get("usage") or {}
            s.update({"calls": ev.get("num_turns"), "subtype": ev.get("subtype"), "costUsd": ev.get("total_cost_usd"),
                      "promptTok": (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0),
                      "cachedTok": u.get("cache_read_input_tokens"), "outTok": u.get("output_tokens"),
                      "final": ev.get("result") or ""})
    return s


def pi_final(jsonl):
    """Last assistant text of a pi --mode json run."""
    last = ""
    for line in open(jsonl, errors="replace"):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        msg = ev.get("message") if isinstance(ev.get("message"), dict) else None
        if ev.get("type") in ("message_end", "turn_end") and msg and msg.get("role") == "assistant":
            txt = "".join(c.get("text", "") for c in msg.get("content", []) if isinstance(c, dict) and c.get("type") == "text")
            if txt.strip():
                last = txt
    return last


def run_task(contender, task, a, log, d=None, prompt=None, ws=None):
    """d, prompt, ws: a review round reuses an existing workspace with a new prompt."""
    t = T.TASKS[task]
    d = d or os.path.join(ROOT, "runs", contender, task)
    if os.path.exists(d):
        shutil.rmtree(d)
    home = new_home(os.path.join(d, "home"), task)
    if ws:  # the diff stays against commit^, even if an earlier round committed
        base = git("rev-parse", t["commit"] + "^", cwd=source(t["repo"])).strip()
    else:
        ws = os.path.join(d, "ws")
        base = prepare(task, ws)
    prompt = prompt or t["prompt"]
    waited, status0 = 0, None
    proxy = None
    ro = []
    if contender == "claude-sonnet":
        envx = claude_env(home, log)
        cmd = [CLAUDE, "--print", "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions",
               "--max-turns", str(a.max_calls), "--model", "sonnet", prompt]
        ro = [CLAUDE]
    else:
        if contender.startswith("strata"):
            waited = wait_strata_idle(log)
            status0 = strata_status()
        proxy = CaptureProxy(upstream(contender), os.path.join(d, "calls.jsonl"), a.max_calls).start()
        ad, provider, model = pi_agent_dir(home, contender, "http://127.0.0.1:%d" % proxy.port)
        envx = {"PI_CODING_AGENT_DIR": ad, "PI_OFFLINE": "1", "PI_TELEMETRY": "0"}
        tools = PI_TOOLS
        if contender == "strata-harness":
            tools += ",plan,finish"
            envx.update({"JUNIOR_MAX_CALLS": str(JUNIOR_CALLS), "JUNIOR_SECONDS": str(a.timeout),
                         "JUNIOR_DEADLINE": str(int(time.time()) + a.timeout), "JUNIOR_REPORT": os.path.join(home, "finish.json"),
                         "JUNIOR_BASE": base})
        cmd = ["pi", "--mode", "json", "-p", "--session-dir", os.path.join(home, "sessions"), "--provider", provider,
               "--model", model, "--thinking", "medium" if contender == "strata-think" else "off", "--tools", tools, prompt]
    argv = S.wrap(cmd, [ws, home], ws, nix=bool(t.get("nix")), ro=ro)
    t0, timed_out = time.time(), False
    with open(os.path.join(d, "agent.jsonl"), "w") as out, open(os.path.join(d, "agent.stderr"), "w") as err:
        p = subprocess.Popen(argv, env=S.env(home, **envx), stdout=out, stderr=err, stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            rc = p.wait(timeout=a.timeout)
        except subprocess.TimeoutExpired:
            timed_out, rc = True, None
            os.killpg(p.pid, 9)
            p.wait()
    wall = round(time.time() - t0)
    if proxy:
        proxy.stop()
    r = {"contender": contender, "task": task, "repo": t["repo"], "commit": t["commit"], "cat": t["cat"], "diff": t["diff"],
         "wallSec": wall, "rc": rc, "timedOut": timed_out, "strataWaitSec": waited, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if proxy:
        w = calls_digest.digest(os.path.join(d, "calls.jsonl"))
        r["wire"] = {k: w.get(k) for k in ("calls", "promptTok", "cachedTok", "outTok", "toolCalls", "toolErrors",
                                            "formatErrors", "maxRepeat", "httpErrors")}
        r["capHit"] = (w.get("calls") or 0) >= a.max_calls
        final = pi_final(os.path.join(d, "agent.jsonl"))
        fj = os.path.join(home, "finish.json")
        if os.path.exists(fj):  # the harness's accepted finish is the final report
            rep = json.load(open(fj))
            r["finish"] = {"forced": rep.get("forced"), "calls": rep.get("calls"), "problems": len(rep.get("problems") or []),
                           "claims": len(rep.get("claims") or []), "verification": len(rep.get("verification") or [])}
            final = json.dumps({k: rep.get(k) for k in ("summary", "files", "claims", "verification", "not_verified", "problems")},
                               ensure_ascii=False, indent=1)
        elif contender == "strata-harness":
            r["finish"] = None
    else:
        c = claude_digest(os.path.join(d, "agent.jsonl"))
        final = c.pop("final", "")
        r["wire"] = c
        r["capHit"] = c.get("subtype") == "error_max_turns"
    if status0 is not None:
        r["strataModel"] = (status0 or {}).get("model") or json.load(urllib.request.urlopen(strata_root() + "/health", timeout=10)).get("model")
    open(os.path.join(d, "final.txt"), "w").write(final)
    # the agent's change against commit^, committed or not, new files included
    git("add", "-A", cwd=ws, check=False)
    diff = git("diff", "--cached", base, "--", ".", ":(exclude)target", cwd=ws, check=False)
    open(os.path.join(d, "agent.diff"), "w").write(diff)
    r["diffLines"] = sum(1 for l in diff.splitlines() if l[:1] in "+-" and l[:3] not in ("+++", "---"))
    r["diffFiles"] = sorted({l[6:] for l in diff.splitlines() if l.startswith("+++ b/")})
    ok, detail = V.verify(task, ws, os.path.join(d, "verify"))
    r.update({"pass": ok, "check": detail})
    shutil.rmtree(os.path.join(ws, "target"), ignore_errors=True)
    shutil.rmtree(os.path.join(d, "verify"), ignore_errors=True)
    shutil.rmtree(os.path.join(home, ".cargo"), ignore_errors=True)
    shutil.rmtree(os.path.join(home, ".claude"), ignore_errors=True)  # holds the OAuth token
    return r


def selftest(task, log):
    d = os.path.join(ROOT, "selftest", task)
    pw, rw = os.path.join(d, "parent"), os.path.join(d, "real")
    prepare(task, pw)
    if T.TASKS[task]["cat"] == "investigate":  # the "real" result is commit^ plus a reference ANSWER.md
        subprocess.run(["cp", "-a", pw, rw], check=True)
        ans = open(os.path.join(HERE, "hidden", task + ".answer.md")).read()
        if task == "k-snapshot-class-why":
            tr = V._snapshot_truth(pw, os.path.join(d, "truth"))
            ans += "\nAFFECTED: " + " ".join(sorted(x.split()[0] for x in tr["affected"])) + "\n"
        open(os.path.join(rw, "ANSWER.md"), "w").write(ans)
    else:
        prepare(task, rw, real=True)
    if T.TASKS[task]["repo"] == "dotfiles":
        V.truth(task, rw, pw, os.path.join(d, "truth"))
    t0 = time.time()
    pok, pdet = V.verify(task, pw, os.path.join(d, "vp"))
    rok, rdet = V.verify(task, rw, os.path.join(d, "vr"))
    good = (not pok) and rok
    log("%-24s %s parent=%s real=%s %ds\n    parent: %s\n    real:   %s" % (
        task, "OK " if good else "BAD", pok, rok, time.time() - t0, pdet[:300], rdet[:300]))
    if good:
        shutil.rmtree(d, ignore_errors=True)
    return good


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("contender", choices=["selftest", "reverify", "strata-nothink", "strata-think", "strata-harness", "gemma", "claude-sonnet"])
    ap.add_argument("--tasks", default=",".join(T.TASKS))
    ap.add_argument("--timeout", type=int, default=1800, help="wall-clock cap per task, seconds")
    ap.add_argument("--of", help="reverify: the contender whose results to recheck")
    ap.add_argument("--max-calls", type=int, default=150, help="model calls per task (proxy 429 / claude --max-turns)")
    a = ap.parse_args()

    def log(msg):
        print(time.strftime("%H:%M:%S"), msg, flush=True)

    S.toolchain()
    todo = a.tasks.split(",")
    if a.contender == "selftest":
        bad = [t for t in todo if not selftest(t, log)]
        log("selftest: %d/%d verifiers OK %s" % (len(todo) - len(bad), len(todo), bad))
        return
    res = os.path.join(HERE, "results")
    os.makedirs(res, exist_ok=True)
    if a.contender == "reverify":
        path = os.path.join(res, a.of + ".jsonl")
        rows = [json.loads(l) for l in open(path)]
        for r in rows:
            d = os.path.join(ROOT, "runs", a.of, r["task"])
            if r["task"] in todo and os.path.isdir(os.path.join(d, "ws")):
                r["pass"], r["check"] = V.verify(r["task"], os.path.join(d, "ws"), os.path.join(d, "verify"))
                shutil.rmtree(os.path.join(d, "verify"), ignore_errors=True)
                shutil.rmtree(os.path.join(d, "ws", "target"), ignore_errors=True)
                log("%-24s %s | %s" % (r["task"], "PASS" if r["pass"] else "FAIL", r["check"][:200]))
        with open(path, "w") as f:
            f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        return
    lock = None
    if a.contender.startswith("strata"):
        lock = open(ROOT + "/strata.lock", "w")
        fcntl.flock(lock, fcntl.LOCK_EX)
    for task in todo:
        r = run_task(a.contender, task, a, log)
        with open(os.path.join(res, a.contender + ".jsonl"), "a") as f:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        w = r.get("wire") or {}
        log("%-24s %s %4ds calls=%s in=%s cached=%s out=%s cap=%s timeout=%s | %s" % (
            task, "PASS" if r["pass"] else "FAIL", r["wallSec"], w.get("calls"), w.get("promptTok"), w.get("cachedTok"),
            w.get("outTok"), r["capHit"], r["timedOut"], r["check"][:160]))


if __name__ == "__main__":
    main()
