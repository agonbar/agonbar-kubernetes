#!/usr/bin/env python3
"""Reviewer-author loop: Strata (pi + harness/junior.ts) delivers, Claude reviews the diff like a PR
reviewer, Strata iterates on the comments. Up to --rounds review/fix rounds per task.

  review_loop.py --of strata-harness --tasks a,b [--rounds 2]

Starts from the kept workspace of an earlier run (/tmp/realtasks/runs/<of>/<task>/ws), copied to
/tmp/realtasks/runs/review/<task>/ws. Each round:
  review<i>/  Claude Code + Sonnet on a scratch copy of the workspace (it may run tests there), in
              sandbox.py's bubblewrap with an isolated HOME; it gets the ticket and the junior's
              finish report, and writes comments that point at problems without giving the fix.
  fix<i>/     pi + Strata + junior.ts on the real workspace, prompt = ticket + the comments.
The hidden verifier runs after every fix. One JSON line per round goes to results/review.jsonl.
"""
import argparse, json, os, re, shutil, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import run as R
import sandbox as S
import tasks as T

REVIEW = """Eres el revisor de un PR que ha hecho un programador junior. El PR intenta resolver este ticket:

<ticket>
{ticket}
</ticket>

El directorio actual es el repo con los cambios del PR ya aplicados. El diff completo está en {diff_path} \
(o con `git diff --cached {base}`). La descripción que escribió el autor está en {pr_path}.

Revisa como en un PR real:
- ¿Resuelve el ticket entero? Compruébalo leyendo el código y, si puedes, ejecutando tests o comandos \
(no hay red a clústeres ni credenciales; es una copia, puedes ensuciarla).
- ¿Hay cambios que el ticket no pide, ficheros basura o borrados no pedidos?
- ¿Las explicaciones de causa del autor son ciertas? Compruébalas en el código.

Reglas de tus comentarios:
- NO escribas la solución ni código. Señala el problema, dónde está (fichero:línea) y qué tendría que \
cumplir el resultado, no cómo implementarlo.
- Como mucho 6 comentarios, los que impidan el merge primero. Nada de estilo si hay problemas de fondo.
- Termina con una línea exacta: `VEREDICTO: APROBADO` si se puede mergear tal cual, o `VEREDICTO: CAMBIOS`."""

FIX = """{ticket}

---
Ya entregaste una primera versión: está en el repo, sin commit (`git diff {base}` la muestra). \
El revisor del PR ha dejado estos comentarios:

<revision>
{comments}
</revision>

Atiende cada comentario. Cambia lo que haga falta; si no estás de acuerdo con alguno, explica por qué con \
evidencia en finish. No rehagas lo que ya está bien."""


def git(*a, cwd):
    return subprocess.run(["git"] + list(a), cwd=cwd, capture_output=True, text=True).stdout


def review(task, ws, base, d, final, a, log):
    t = T.TASKS[task]
    if os.path.exists(d):
        shutil.rmtree(d)
    os.makedirs(d)
    cws, home = os.path.join(d, "ws"), os.path.join(d, "home")
    os.makedirs(home)
    subprocess.run(["cp", "-a", "--reflink=auto", ws, cws], check=True)
    git("add", "-A", cwd=cws)
    diff_path, pr_path = os.path.join(d, "pr.diff"), os.path.join(d, "pr.md")
    open(diff_path, "w").write(git("diff", "--cached", base, cwd=cws))
    open(pr_path, "w").write(final or "(el autor no dejó descripción)")
    prompt = REVIEW.format(ticket=t["prompt"], diff_path=diff_path, base=base, pr_path=pr_path)
    envx = R.claude_env(home, log)
    cmd = [R.CLAUDE, "--print", "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions",
           "--max-turns", "40", "--model", "sonnet", prompt]
    argv = S.wrap(cmd, [cws, home], cws, nix=bool(t.get("nix")), ro=[R.CLAUDE, diff_path, pr_path])
    t0 = time.time()
    with open(os.path.join(d, "agent.jsonl"), "w") as out, open(os.path.join(d, "agent.stderr"), "w") as err:
        p = subprocess.Popen(argv, env=S.env(home, **envx), stdout=out, stderr=err, stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            p.wait(timeout=a.review_timeout)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, 9)
            p.wait()
    c = R.claude_digest(os.path.join(d, "agent.jsonl"))
    text = c.pop("final", "")
    open(os.path.join(d, "review.md"), "w").write(text)
    shutil.rmtree(os.path.join(home, ".claude"), ignore_errors=True)  # holds the OAuth token
    shutil.rmtree(cws, ignore_errors=True)
    m = re.findall(r"VEREDICTO:\s*(APROBADO|CAMBIOS)", text)
    return {"verdict": m[-1] if m else None, "wallSec": round(time.time() - t0), "claude": c}, text


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--of", default="strata-harness")
    ap.add_argument("--tasks", required=True)
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=1800, help="Strata fix round wall-clock cap, seconds")
    ap.add_argument("--review-timeout", type=int, default=900)
    ap.add_argument("--max-calls", type=int, default=110)
    a = ap.parse_args()

    def log(msg):
        print(time.strftime("%H:%M:%S"), msg, flush=True)

    out = os.path.join(HERE, "results", "review.jsonl")
    for task in a.tasks.split(","):
        t = T.TASKS[task]
        src = os.path.join(R.ROOT, "runs", a.of, task)
        root = os.path.join(R.ROOT, "runs", "review", task)
        ws = os.path.join(root, "ws")
        if os.path.exists(root):
            shutil.rmtree(root)
        os.makedirs(root)
        subprocess.run(["cp", "-a", "--reflink=auto", os.path.join(src, "ws"), ws], check=True)
        base = git("rev-parse", t["commit"] + "^", cwd=R.source(t["repo"])).strip()
        final = open(os.path.join(src, "final.txt")).read() if os.path.exists(os.path.join(src, "final.txt")) else ""
        for i in range(1, a.rounds + 1):
            rv, comments = review(task, ws, base, os.path.join(root, "review%d" % i), final, a, log)
            log("%-24s review%d %s %ds claude=%s" % (task, i, rv["verdict"], rv["wallSec"], rv["claude"]))
            row = {"task": task, "round": i, "review": rv, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
            if rv["verdict"] != "CAMBIOS":
                with open(out, "a") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                break
            prompt = FIX.format(ticket=t["prompt"], base=base, comments=comments.strip())
            fd = os.path.join(root, "fix%d" % i)
            r = R.run_task("strata-harness", task, a, log, d=fd, prompt=prompt, ws=ws)
            final = open(os.path.join(fd, "final.txt")).read()
            row["fix"] = {k: r.get(k) for k in ("pass", "check", "wallSec", "wire", "capHit", "timedOut", "finish", "diffLines", "diffFiles")}
            with open(out, "a") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            w = r.get("wire") or {}
            log("%-24s fix%d %s %ds calls=%s | %s" % (task, i, "PASS" if r["pass"] else "FAIL", r["wallSec"], w.get("calls"), r["check"][:160]))


if __name__ == "__main__":
    main()
