"""Objective verifiers, one per task. The agent never sees them.

  verify(task_id, ws, scratch) -> (passed, detail)

They run on a copy of the workspace (hidden tests are added to the copy) and execute the
agent's code only inside the sandbox. `--self-test` runs each one on commit^ (must fail)
and on the real commit (must pass), which is how the verifiers themselves were checked.
"""
import json, os, re, shlex, shutil, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sandbox as S

HERE = os.path.dirname(os.path.abspath(__file__))
HIDDEN = os.path.join(HERE, "hidden")
# Expected values that come from evaluating the private dotfiles flake (commit^ or the real
# commit) are computed by `run.py selftest` into TRUTH, never stored in this public repo.
TRUTH = os.environ.get("REALTASKS_TRUTH", "/tmp/realtasks/truth")


def sh(cmd, ws, scratch, timeout=600, nix=False, **kw):
    p = S.run(["bash", "-c", cmd], ws, os.path.join(scratch, "vhome"), nix=nix, timeout=timeout, rw=(scratch,), **kw)
    with open(os.path.join(scratch, "sh.log"), "a") as f:  # kept next to the run when a check fails
        f.write("$ %s\nrc=%s PATH=%s\n%s\n%s\n\n" % (cmd[:300], p.returncode, json.dumps({"argv": S.LAST[0][:-3], "PATH": S.LAST[1]["PATH"]}),
                                                    (p.stdout or "")[-3000:], (p.stderr or "")[-3000:]))
    return p


def tail(p, n=400):
    return ((p.stdout or "") + (p.stderr or ""))[-n:].replace("\n", " | ")


def writex(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").write(text)
    os.chmod(path, 0o755)


def py():
    return S.tool("pyenv") + "/bin/python3"


def block_scalar(yaml_text, key):
    """The text of a `key: |` block scalar (a ConfigMap script), dedented."""
    lines = yaml_text.splitlines()
    for i, l in enumerate(lines):
        m = re.match(r"^(\s*)%s:\s*\|" % re.escape(key), l)
        if m:
            out, ind = [], None
            for l2 in lines[i + 1:]:
                if l2.strip() == "":
                    out.append("")
                    continue
                cur = len(l2) - len(l2.lstrip())
                if cur <= len(m.group(1)):
                    break
                ind = cur if ind is None else ind
                out.append(l2[ind:])
            return "\n".join(out) + "\n"
    return None


def kubeconform(ws, scratch, path):
    p = sh("kubeconform -strict -summary -ignore-missing-schemas %s" % shlex.quote(path), ws, scratch, timeout=300)
    return p.returncode == 0, tail(p, 200)


# ------------------------------------------------------------------ kubernetes

def v_k_backup_deleted_pvc(ws, scratch):
    y = open(os.path.join(ws, "system/snapshots/backup.yaml")).read()
    script = block_scalar(y, "backup.sh")
    if not script:
        return False, "backup.sh block not found"
    t = os.path.join(scratch, "bk")
    os.makedirs(t, exist_ok=True)
    open(os.path.join(t, "backup.sh"), "w").write(script)
    # kubectl stub: any `get pvc <name>` is NotFound (the PVC was deleted); everything else
    # answers empty and succeeds. Every call is logged.
    writex(os.path.join(t, "bin/kubectl"), """#!/usr/bin/env bash
echo "$*" >> %s/kubectl.log
args=("$@"); for i in "${!args[@]}"; do
  if [ "${args[$i]}" = get ] && [ "${args[$((i+1))]:-}" = pvc ]; then
    n="${args[$((i+2))]:-}"; case "$n" in ""|-*) exit 0 ;; esac
    case " $* " in *" --ignore-not-found "*|*" --ignore-not-found=true "*) exit 0 ;; esac  # as real kubectl
    echo "Error from server (NotFound): persistentvolumeclaims \\"$n\\" not found" >&2; exit 1
  fi
done
exit 0
""" % t)
    for stub in ("sleep",):
        writex(os.path.join(t, "bin", stub), "#!/bin/sh\nexit 0\n")
    p = sh("bash -n backup.sh", t, scratch)
    if p.returncode:
        return False, "bash -n: " + tail(p)
    # 1. the function on its own, under set -e: a deleted PVC returns, the caller goes on, FAILED
    # untouched. lib.sh = every definition of the script, without the list of calls and the exit.
    lib = re.sub(r"^\s*backup_\w+\s+\S+\s+\S+\s*$", "", script, flags=re.M)
    open(os.path.join(t, "lib.sh"), "w").write(re.sub(r"^exit\b.*$", "", lib, flags=re.M))
    fn = sh("""set +e; source ./lib.sh >/dev/null 2>&1; set -e; FAILED=0
backup_pvc piracy anisub-data; echo "REACHED FAILED=${FAILED}" """, t, scratch, timeout=60, extra_path=[t + "/bin"])
    reached = "REACHED" in fn.stdout
    failed0 = "REACHED FAILED=0" in fn.stdout
    # 2. the whole script with every listed PVC missing: it must try all of them
    listed = re.findall(r"^\s*backup_\w+\s+(\S+)\s+(\S+)\s*$", script, re.M)
    if os.path.exists(t + "/kubectl.log"):
        os.remove(t + "/kubectl.log")
    full = sh("bash backup.sh", t, scratch, timeout=180, extra_path=[t + "/bin"])
    log = open(t + "/kubectl.log").read() if os.path.exists(t + "/kubectl.log") else ""
    asked = sum(1 for ns, pvc in listed if re.search(r"get pvc %s\b" % re.escape(pvc), log))
    kc, kd = kubeconform(ws, scratch, "system/snapshots/backup.yaml")
    ok = reached and failed0 and asked == len(listed) and kc
    return ok, "fn_continues=%s failed_stays_0=%s pvcs_tried=%d/%d full_rc=%s kubeconform=%s" % (
        reached, failed0, asked, len(listed), full.returncode, kc)


def _lst(path, rows):
    with open(path, "wb") as f:
        for size, p in rows:
            f.write(("%d\t1700000000.0\t%s" % (size, p)).encode() + b"\0")


def _dump(d):
    out = {}
    for fn in os.listdir(d) if os.path.isdir(d) else []:
        out[fn[:-4]] = {l.split("\t")[1]: l.rstrip("\n").split("\t")[2] for l in open(os.path.join(d, fn)) if l.strip()}
    return out


def v_k_nas_diff_extra(ws, scratch):
    t = os.path.join(scratch, "nd")
    os.makedirs(t, exist_ok=True)
    _lst(t + "/a.lst", [(5, "x/same.mkv"), (100, "x/moved.mkv"), (200, "x/gone.mkv"), (300, "x/uniq.bin"),
                        (50, "x/samepath.dat")])
    _lst(t + "/b.lst", [(5, "x/same.mkv"), (100, "y/moved.mkv")])
    _lst(t + "/e1.lst", [(200, "/mnt/RAID/adrian/z/gone.mkv"), (999, "x/samepath.dat")])
    _lst(t + "/e2.lst", [(7, "/mnt/RAID/nas00-rescate/w.txt")])
    script = os.path.join(ws, "scripts/nas-public-diff.py")
    runs = {
        "noextra": "a.lst b.lst --dump d0",
        "extra": "a.lst b.lst e1.lst e2.lst --dump d1",
        "dumpfirst": "--dump d2 a.lst b.lst e1.lst e2.lst",
    }
    res, notes = {}, []
    for name, args in runs.items():
        p = sh("python3 %s %s" % (shlex.quote(script), args), t, scratch, timeout=60)
        if p.returncode:
            return False, "%s rc=%s %s" % (name, p.returncode, tail(p))
        res[name] = (_dump(os.path.join(t, args.split("--dump ")[1].split()[0])), p.stdout)
    d0, d1, d2 = res["noextra"][0], res["extra"][0], res["dumpfirst"][0]
    checks = {
        "noextra_gone_unique": "x/gone.mkv" in d0.get("unique", {}),
        # a separate class for extra hits ("moved-extra") is as good as "moved"
        "extra_gone_moved": any(m.get("x/gone.mkv", "").endswith("z/gone.mkv") for c, m in d1.items() if c.startswith("moved")),
        "extra_moved_still_B": d1.get("moved", {}).get("x/moved.mkv") == "y/moved.mkv",
        "extra_uniq_unique": "x/uniq.bin" in d1.get("unique", {}),
        "extra_not_path_B": "x/samepath.dat" in d1.get("unique", {}) and "same path, different size: 0" in res["extra"][1],
        "dump_any_position": d2 == d1,
    }
    return all(checks.values()), " ".join("%s=%s" % kv for kv in checks.items())


def v_k_autotaint_deployment(ws, scratch):
    f = "system/node-autotaint/node-autotaint.yaml"
    kc, kd = kubeconform(ws, scratch, f)
    p = sh("%s -c 'import yaml,json,sys; print(json.dumps([d for d in yaml.safe_load_all(open(sys.argv[1])) if d]))' %s"
           % (py(), f), ws, scratch)
    if p.returncode:
        return False, "yaml: " + tail(p)
    docs = json.loads(p.stdout)
    kinds = [d.get("kind") for d in docs]
    deps = [d for d in docs if d.get("kind") == "Deployment"]
    c = {"kubeconform": kc, "no_cronjob": "CronJob" not in kinds and "Job" not in kinds, "one_deployment": len(deps) == 1}
    if len(deps) == 1:
        d = deps[0]
        spec = d.get("spec", {})
        pod = spec.get("template", {}).get("spec", {})
        conts = pod.get("containers") or [{}]
        cmd = " ".join(str(x) for cn in conts for x in (cn.get("command") or []) + (cn.get("args") or []))
        cm = next((x for x in docs if x.get("kind") == "ConfigMap"), {})
        script = json.dumps(cm.get("data", {}))
        envs = {e.get("name") for cn in conts for e in cn.get("env") or []}
        cmd += " " + " ".join(str(e.get("value", "")) for cn in conts for e in cn.get("env") or [])  # sleep "$INTERVAL"
        c.update({
            "replicas_1": spec.get("replicas", 1) == 1,
            "sa_kept": pod.get("serviceAccountName") == "node-autotaint",
            "runs_reconcile": "reconcile" in cmd or "reconcile" in script,
            "loop_120s": "120" in cmd + script and re.search(r"\b(while|until|for)\b", cmd + script) is not None,
            "env_kept": "UNREADY_THRESHOLD_SECONDS" in envs,
            "rbac_kept": {"ServiceAccount", "ClusterRole", "ClusterRoleBinding", "ConfigMap"} <= set(kinds),
        })
    return all(c.values()), " ".join("%s=%s" % kv for kv in c.items())


SEAL_DIRS = ["system/argocd", "system/democratic-csi", "deployments/agonbar", "deployments/aya", "deployments/dawarich",
             "deployments/games", "deployments/immich", "deployments/knowledge", "deployments/lamg", "deployments/piracy",
             "deployments/trek", "deployments/yavoo"]


def v_k_seal_secrets_shrink(ws, scratch):
    src = os.path.join(ws, "seal-secrets.sh")
    stub = """#!/usr/bin/env bash
f=""; while [ $# -gt 0 ]; do case "$1" in -f) f=$2; shift 2 ;; -f=*) f=${1#-f=}; shift ;; *) shift ;; esac; done
if [ -n "$f" ]; then in=$(cat "$f") || exit 1; else in=$(cat); fi
n=$(printf '%s\\n' "$in" | grep -c '^kind: Secret')
for i in $(seq 1 "$n"); do printf -- '---\\napiVersion: bitnami.com/v1alpha1\\nkind: SealedSecret\\nmetadata:\\n  name: stub%s\\n' "$i"; done
printf '%s' "$in" | grep -q FAILME && { echo "error: cannot fetch certificate" >&2; exit 1; }
exit 0
"""

    def tree(name):
        t = os.path.join(scratch, name)
        shutil.copytree(ws, t, symlinks=True, ignore=shutil.ignore_patterns(".git", "target"))
        writex(os.path.join(t, ".stub/kubeseal"), stub)
        return t

    def sealed_count(path):
        return sum(1 for l in open(path) if l.startswith("kind: SealedSecret")) if os.path.exists(path) else 0

    # the 12 directories seal-secrets.sh handles at commit^ (the agent may rewrite how it lists them)
    dirs = [d for d in SEAL_DIRS if os.path.exists(os.path.join(ws, d, "sealedsecrets.yaml"))]
    multi = [d for d in dirs if sealed_count(os.path.join(ws, d, "sealedsecrets.yaml")) >= 2]
    if len(multi) < 3:
        return False, "fixture: need 3 dirs with >=2 sealed secrets, got %s" % multi
    shrink, equal, fail = multi[:3]
    secret = "---\napiVersion: v1\nkind: Secret\nmetadata:\n  name: s%d\nstringData:\n  k: v\n"

    def put(t, d, n, failme=False):
        open(os.path.join(t, d, "secrets.yaml"), "w").write("".join(secret % i for i in range(n)) + ("# FAILME\n" if failme else ""))

    def snap(t):
        return {d: open(os.path.join(t, d, "sealedsecrets.yaml"), "rb").read() for d in dirs}

    interp = (open(src).readline()[2:].strip() or "/bin/sh").split()
    run_cmd = " ".join(shlex.quote(x) for x in interp) + " ./seal-secrets.sh"
    t = tree("seal1")
    put(t, shrink, sealed_count(os.path.join(t, shrink, "sealedsecrets.yaml")) - 1)
    put(t, equal, sealed_count(os.path.join(t, equal, "sealedsecrets.yaml")))
    put(t, fail, 5, failme=True)
    before = snap(t)
    p = sh(run_cmd, t, scratch, timeout=120, extra_path=[t + "/.stub"])
    after = snap(t)
    c = {
        "exit_nonzero": p.returncode != 0,
        "shrink_untouched": after[shrink] == before[shrink],
        "shrink_reported": shrink.split("/")[-1] in p.stdout + p.stderr,
        "equal_replaced": b"name: stub1" in after[equal],
        "failed_kubeseal_untouched": after[fail] == before[fail],
        "missing_secrets_untouched": all(after[d] == before[d] for d in dirs if d not in (shrink, equal, fail)),
    }
    t2 = tree("seal2")
    put(t2, equal, sealed_count(os.path.join(t2, equal, "sealedsecrets.yaml")) + 1)
    b2 = snap(t2)
    p2 = sh("DRY_RUN=1 " + run_cmd, t2, scratch, timeout=120, extra_path=[t2 + "/.stub"])
    c["dry_run_writes_nothing"] = snap(t2) == b2 and p2.returncode in (0, 1)
    return all(c.values()), " ".join("%s=%s" % kv for kv in c.items())


def _snapshot_truth(ws, scratch):
    """Listed PVCs whose manifest's storageClassName is not the job's global SNAPSHOT_CLASS's class."""
    p = sh(r"""%s - <<'EOF'
import yaml, glob, re, json
script = open('system/snapshots/backup.yaml').read()
glob_cls = re.search(r'SNAPSHOT_CLASS="([^"]+)"', script).group(1)
listed = re.findall(r'^\s*backup_pvc\s+(\S+)\s+(\S+)\s*$', script, re.M)
cls = {}
for f in glob.glob('deployments/**/*.y*ml', recursive=True) + glob.glob('system/**/*.y*ml', recursive=True):
    try:
        docs = list(yaml.safe_load_all(open(f)))
    except Exception:
        continue
    for d in docs:
        if isinstance(d, dict) and d.get('kind') == 'PersistentVolumeClaim':
            m = d.get('metadata', {})
            cls.setdefault(m.get('name'), []).append((m.get('namespace'), d.get('spec', {}).get('storageClassName')))
out = []
for ns, pvc in listed:
    c = [k for n, k in cls.get(pvc, []) if n in (ns, None)] or [k for n, k in cls.get(pvc, [])]
    if c and c[0] and c[0] != glob_cls:
        out.append('%%s/%%s %%s' %% (ns, pvc, c[0]))
print(json.dumps({'global': glob_cls, 'affected': out}))
EOF""" % py(), ws, scratch)
    return json.loads(p.stdout)


def v_k_snapshot_class_why(ws, scratch):
    ans = os.path.join(ws, "ANSWER.md")
    if not os.path.exists(ans):
        return False, "no ANSWER.md"
    text = open(ans).read()
    truth = _snapshot_truth(ws, scratch)
    want = {a.split()[0] for a in truth["affected"]}
    m = re.findall(r"AFFECTED:\s*(.*)", text)
    got = set(re.findall(r"[\w-]+/[\w.-]+", m[-1])) if m else set()
    tp = len(got & want)
    cause = "truenas-iscsi-ssd-lan" in text and ("SNAPSHOT_CLASS" in text or "truenas-iscsi-ssd\"" in text or "truenas-iscsi-ssd`" in text)
    other = [l for l in subprocess.run(["git", "-C", ws, "status", "--porcelain"], capture_output=True, text=True).stdout.splitlines()
             if not l.endswith("ANSWER.md")]
    ok = got == want and cause and not other
    return ok, "affected tp=%d got=%d want=%d cause=%s untouched=%s classes=%s" % (
        tp, len(got), len(want), cause, not other, sorted({a.split()[1] for a in truth["affected"]}))


# ------------------------------------------------------------------ dotfiles

def v_d_paperclip_env(ws, scratch):
    script = os.path.join(ws, "nix/home/modules/pi/paperclip-sandbox.sh")
    t = os.path.join(scratch, "pp")
    home = os.path.join(t, "home")
    os.makedirs(home + "/.pi/agent/extensions", exist_ok=True)
    for f in ("models.json", "settings.json", "extensions/web.ts"):
        open(os.path.join(home, ".pi/agent", f), "w").write("{}")
    # pasta/bwrap stubs run what follows `--`; pi dumps its environment.
    writex(t + "/bin/pasta", '#!/usr/bin/env bash\nwhile [ "$1" != -- ]; do shift; done; shift; exec "$@"\n')
    # bwrap is resolved with `command -v` before `env -i`, so the stub sees exactly pi's environment
    writex(t + "/bin/bwrap", "#!/run/current-system/sw/bin/bash\nenv > %s/pi.env\n" % t)
    writex(t + "/bin/pi-paperclip-api-filter", "#!/bin/sh\necho 41234\n")
    bash = S.tool("bash-ni") + "/bin/bash"  # writeShellApplication's shell: no compgen, no readline
    p = sh("cd %s && HOME=%s PAPERCLIP_API_URL=http://127.0.0.1:3100 PAPERCLIP_AGENT_ID=a1 PAPERCLIP_RUN_ID=r1 "
           "PAPERCLIP_RUNTIME_TOOLS_X=hidden %s %s -p hi" % (t, home, bash, shlex.quote(script)), ws, scratch, timeout=60,
           extra_path=[t + "/bin"])
    envtxt = open(t + "/pi.env").read() if os.path.exists(t + "/pi.env") else ""
    sc = sh("shellcheck -s bash %s" % shlex.quote(script), ws, scratch)  # writeShellApplication runs it at build
    c = {"pi_ran": bool(envtxt), "api_url": "PAPERCLIP_API_URL=http://127.0.0.1:3100" in envtxt,
         "agent_id": "PAPERCLIP_AGENT_ID=a1" in envtxt, "run_id": "PAPERCLIP_RUN_ID=r1" in envtxt,
         "runtime_tools_dropped": "PAPERCLIP_RUNTIME_TOOLS_X" not in envtxt, "shellcheck": sc.returncode == 0}
    return all(c.values()), " ".join("%s=%s" % kv for kv in c.items()) + ("" if envtxt else " | " + tail(p, 200)) + (
        "" if sc.returncode == 0 else " | sc: " + tail(sc, 200))


GUARD_CASES = [
    (0, "ls -la"),
    (0, "kubectl --context lamg -n agonbar get deploy banderillo"),
    (2, "kubectl --context lamg -n agonbar scale deploy/banderillo --replicas=1"),
    (2, "kubectl --context lamg -n agonbar scale deployment banderillo --replicas=1"),
    (2, "kubectl --context lamg -n agonbar set image deploy/banderillo x=y:1"),
    (2, "kubectl --context lamg -n agonbar annotate deploy banderillo foo=bar --overwrite"),
    (2, "kubectl --context lamg -n agonbar patch deploy banderillo -p '{\"spec\":{}}'"),
    (0, "kubectl --context lamg -n agonbar scale deploy/refranillo --replicas=1"),
    (2, "kubectl --context sensei-ovh -n alloy-logs annotate secret gke-loki-basic-auth x=y"),
    (2, "kubectl --context gke-agent -n prd scale deploy/chino --replicas=1"),
    (0, "kubectl --context lamg -n agonbar rollout restart deploy/banderillo"),
    (0, "kubectl --context lamg -n agonbar delete pod banderillo-x"),
    (0, "kubectl --context lamg -n agonbar scale deploy/does-not-exist --replicas=1"),
    (0, "echo kubectl label is a word"),
    (0, "ALLOW_LIVE_WRITE=1 kubectl --context lamg -n agonbar scale deploy/banderillo --replicas=1"),
    (2, "kubectl --context=lamg -n agonbar label deploy banderillo a=b"),
    (2, "kubectl --context lamg -n agonbar get deploy banderillo && kubectl --context lamg -n agonbar scale deploy/banderillo --replicas=1"),
    (0, "kubectl --context=sensei-ovh -n infra exec deploy/paperclip -- sh -c 'pc agent skills:sync x --mode replace'"),
    (0, "kubectl --context=sensei-ovh -n infra exec deploy/paperclip -- sh -c 'set=a; grep -rn router.patch src'"),
    (0, "kubectl --context=sensei-ovh -n infra exec deploy/paperclip -- node -e 'c=c.replace(a,b)'"),
    (2, "kubectl -n agonbar --context lamg label deploy/banderillo a=b"),
    (2, "cd /tmp; kubectl --context lamg -n agonbar edit deploy/banderillo"),
]


def v_d_guard_subcommand(ws, scratch):
    hook = os.path.join(ws, "nix/home/modules/claude-hooks/kubectl-live-write-guard.sh")
    t = os.path.join(scratch, "guard")
    # kubectl stub with the live objects the real test uses: owners||instance label
    writex(t + "/bin/kubectl", """#!/usr/bin/env bash
for a in "$@"; do case "$a" in
  banderillo|chino|paperclip) echo -n "||$a"; exit 0 ;;
  refranillo) echo -n "||"; exit 0 ;;
  gke-loki-basic-auth) echo -n "ExternalSecret ||"; exit 0 ;;
esac; done
echo "Error from server (NotFound)" >&2; exit 1
""")
    bad = []
    for want, cmd in GUARD_CASES:
        inp = json.dumps({"tool_input": {"command": cmd}, "cwd": "/tmp"})
        p = sh("printf '%%s' %s | bash %s" % (shlex.quote(inp), shlex.quote(hook)), ws, scratch, timeout=30, extra_path=[t + "/bin"])
        if p.returncode != want:
            bad.append("rc=%s want=%s: %s" % (p.returncode, want, cmd[:70]))
    tests = open(os.path.join(ws, "nix/home/modules/claude-hooks/tests/kubectl-live-write-guard.sh")).read()
    added = sum(1 for k in ("--mode replace", "router.patch", "c.replace(") if k in tests)
    ok = not bad and added == 3
    return ok, "cases %d/%d tests_added=%d/3 %s" % (len(GUARD_CASES) - len(bad), len(GUARD_CASES), added, "; ".join(bad)[:300])


def v_d_whisper_newlines(ws, scratch):
    d = os.path.join(ws, "nix/pkgs/wyoming-whisper-bridge")
    shutil.copy(os.path.join(HIDDEN, "d_whisper_test.go"), os.path.join(d, "zz_hidden_test.go"))
    p = sh("cd %s && go vet ./... && go test -count=1 ./... 2>&1 | tail -30" % shlex.quote(d), ws, scratch, timeout=600)
    hidden = sh("cd %s && go test -count=1 -run Hidden -v ./... 2>&1 | grep -E '^(--- |ok|FAIL|PASS)'" % shlex.quote(d), ws, scratch, timeout=600)
    ok = p.returncode == 0 and "FAIL" not in p.stdout and "--- FAIL" not in hidden.stdout and "PASS" in hidden.stdout
    return ok, "all_tests=%s hidden=%s" % ("FAIL" not in p.stdout and p.returncode == 0, tail(hidden, 300))


NIXEVAL = "cd nix && nix eval --json --no-write-lock-file "


def _nix_json(ws, scratch, attr, timeout=900):
    p = sh(NIXEVAL + shlex.quote(".#" + attr), ws, scratch, timeout=timeout, nix=True)
    if p.returncode:
        raise RuntimeError(tail(p, 300))
    return json.loads(p.stdout)


def _systemd_env(entries):
    """What systemd makes of a list of Environment= values: quotes group, unquoted spaces split."""
    out = {}
    for e in entries:
        for w in shlex.split(e):
            k, _, v = w.partition("=")
            out[k] = v
    return out


MEETING = "nixosConfigurations.work-vm-00.config.home-manager.users.agonbar.systemd.user.services"


def v_d_meeting_env_quote(ws, scratch):
    expr = MEETING + ".meeting-bot-scheduler.Service.Environment"
    try:
        got = _systemd_env(_nix_json(ws, scratch, expr))
        cal = _systemd_env(_nix_json(ws, scratch, MEETING + ".meeting-bot-calendar-sync.Service.Environment"))
    except RuntimeError as e:
        return False, "nix eval: %s" % e
    want = json.load(open(os.path.join(TRUTH, "d-meeting-env-quote.json")))
    c = {"scheduler_env": got == want["scheduler"], "calendar_env": cal == want["calendar"],
         "meeting_only": got.get("MEETING_ONLY") == "^Daily SenseiVision$"}
    return all(c.values()), " ".join("%s=%s" % kv for kv in c.items())


def v_d_rebuild_nopasswd(ws, scratch):
    want = json.load(open(os.path.join(TRUTH, "d-rebuild-nopasswd.json")))
    got = {}
    try:
        for h in ("t480", "asus-rog", "work-vm-00"):
            got[h] = _nix_json(ws, scratch, "nixosConfigurations.%s.config.security.sudo.configFile" % h)
    except RuntimeError as e:
        return False, "nix eval: %s" % e
    hosts = open(os.path.join(ws, "nix/hosts/t480/configuration.nix")).read() + open(os.path.join(ws, "nix/hosts/asus-rog/configuration.nix")).read()
    mods = [f for f in os.listdir(os.path.join(ws, "nix/modules/nixos"))
            if "switch-to-configuration" in open(os.path.join(ws, "nix/modules/nixos", f)).read()] if os.path.isdir(os.path.join(ws, "nix/modules/nixos")) else []
    c = {"t480_unchanged": got["t480"] == want["t480"], "asus_eq_t480": got["asus-rog"] == want["t480"],
         "workvm_unchanged": got["work-vm-00"] == want["work-vm-00"], "shared_module": len(mods) == 1,
         "no_copy_in_hosts": "switch-to-configuration" not in hosts}
    return all(c.values()), " ".join("%s=%s" % kv for kv in c.items())


def v_d_gke_context_why(ws, scratch):
    ans = os.path.join(ws, "ANSWER.md")
    if not os.path.exists(ans):
        return False, "no ANSWER.md"
    t = open(ans).read()
    c = {"defined_in_server_nix": "server.nix" in t, "workvm00_uses_agonbar_nix": "agonbar.nix" in t,
         # flake.nix at commit^ gives server.nix to work-vm-01, kubernetes-10 and kubernetes-20 (the
         # commit message still calls them work-vm-01/02/03, names from before a rename)
         "headless_vms_named": len(set(re.findall(r"work-vm-01|kubernetes-10|kubernetes-20", t))) >= 2,
         "kubeconfig_var": "KUBECONFIG" in t,
         "move_to_shared_module": bool(re.search(r"home/modules/\S+\.nix|m[oó]dulo (com[uú]n|compartido)|shared module", t))}
    other = [l for l in subprocess.run(["git", "-C", ws, "status", "--porcelain"], capture_output=True, text=True).stdout.splitlines()
             if not l.endswith("ANSWER.md")]
    c["untouched"] = not other
    return all(c.values()), " ".join("%s=%s" % kv for kv in c.items())


# ------------------------------------------------------------------ benilla

def _cargo(ws, scratch, crate, hidden_file, target_rel):
    """The crate's whole test suite must pass, and so must every hidden test appended to it."""
    tgt = os.path.join(ws, target_rel)
    hidden = open(os.path.join(HIDDEN, hidden_file)).read()
    n_hidden = len(re.findall(r"#\[test\]", hidden))
    open(tgt, "a").write(hidden)
    seed, home_cargo = "/tmp/realtasks/cargo-home", os.path.join(scratch, "vhome", ".cargo")
    os.makedirs(os.path.dirname(home_cargo), exist_ok=True)
    if os.path.isdir(seed) and not os.path.exists(home_cargo):  # crates already fetched: no registry round trip
        subprocess.run(["cp", "-a", seed, home_cargo], check=False)
    p = sh("command -v cargo >/dev/null || { echo \"no cargo; PATH=$PATH\"; ls -la %s; }; cargo test -p %s 2>&1" % (S.TC_DIR, crate), ws, scratch, timeout=1800)
    out = p.stdout + p.stderr
    results = re.findall(r"test result: (\w+)\. (\d+) passed; (\d+) failed", out)
    # a module can be compiled into several test targets, so count at least n_hidden and no failure
    hidden_ok = len(re.findall(r"^test \S*rthidden_\S* \.\.\. ok$", out, re.M)) >= n_hidden and not re.search(
        r"^test \S*rthidden_\S* \.\.\. FAILED$", out, re.M)
    all_ok = p.returncode == 0 and bool(results) and all(r[0] == "ok" and r[2] == "0" for r in results)
    fails = re.findall(r"^test (\S+) \.\.\. FAILED$", out, re.M)
    err = "" if results else tail(p, 300)
    return all_ok and hidden_ok, "all=%s hidden=%s/%d passed=%d failed=%s %s" % (
        all_ok, len(re.findall(r"^test \S*rthidden_\S* \.\.\. ok$", out, re.M)), n_hidden,
        sum(int(r[1]) for r in results), fails[:6], err)


def v_b_tradeskill_nummade(ws, scratch):
    return _cargo(ws, scratch, "benilla-ui", "b_tradeskill.rs", "crates/benilla-ui/src/script/tradeskill/tests.rs")


def v_b_dial_ipv4(ws, scratch):
    return _cargo(ws, scratch, "benilla-protocol", "b_dial.rs", "crates/benilla-protocol/src/lib.rs")


def v_b_light_band_cycle(ws, scratch):
    return _cargo(ws, scratch, "benilla-formats", "b_light.rs", "crates/benilla-formats/src/light/tables.rs")


def _verify_once(task, ws, scratch):
    if os.path.exists(scratch):
        shutil.rmtree(scratch)
    os.makedirs(scratch)
    copy = os.path.join(scratch, "ws")
    subprocess.run(["cp", "-a", "--reflink=auto", ws, copy], check=True)
    fn = globals()["v_" + task.replace("-", "_")]
    try:
        return fn(copy, scratch)
    except Exception as e:  # a verifier crash on odd agent output is a fail, with the reason
        return False, "verifier error: %r" % e


def verify(task, ws, scratch):
    """Copies ws to scratch/ws (hidden tests go there) and runs the task's verifier. A fail is
    rerun once from a fresh copy (network for kubeconform schemas and crates can blip); if the
    two disagree the detail says so. The failing attempt's command log stays in scratch.fail.log."""
    ok, detail = _verify_once(task, ws, scratch)
    if ok:
        return ok, detail
    if os.path.exists(os.path.join(scratch, "sh.log")):
        shutil.copy(os.path.join(scratch, "sh.log"), scratch.rstrip("/") + ".fail.log")
    time.sleep(60)
    ok2, detail2 = _verify_once(task, ws, scratch)
    return ok2, detail2 if ok2 == ok else "FLAKY(1st fail: %s) %s" % (detail[:200], detail2)


def truth(task, real_ws, parent_ws, scratch):
    """Expected values for the dotfiles Nix tasks, from the real commit and its parent."""
    os.makedirs(TRUTH, exist_ok=True)
    if task == "d-meeting-env-quote":
        t = {"scheduler": _systemd_env(_nix_json(real_ws, scratch, MEETING + ".meeting-bot-scheduler.Service.Environment")),
             "calendar": _systemd_env(_nix_json(real_ws, scratch, MEETING + ".meeting-bot-calendar-sync.Service.Environment"))}
    elif task == "d-rebuild-nopasswd":
        t = {h: _nix_json(parent_ws, scratch, "nixosConfigurations.%s.config.security.sudo.configFile" % h) for h in ("t480", "work-vm-00")}
    else:
        return
    json.dump(t, open(os.path.join(TRUTH, task + ".json"), "w"))
