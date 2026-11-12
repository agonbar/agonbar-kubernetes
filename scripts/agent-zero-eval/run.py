#!/usr/bin/env python3
"""Run a teacher's task set against the lamg Agent Zero and record the results.

Usage: scripts/agent-zero-eval/run.py [task ...]   (default: all tasks)

Port-forwards svc/agent-zero, reads the API token from the pod, sends each task
to /api_message in a fresh chat, waits for the answer (the endpoint is
synchronous) and writes, per task: wall time, the reply, which expected files
exist in /a0/usr/workdir, and the Strata gateway's per-priority counters before
and after. Output goes to ./out/<timestamp>/ next to this script.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

CTX = "lamg"
NS = "agent-zero"
PORT = 18080
HERE = os.path.dirname(os.path.abspath(__file__))

TASKS = {
    "slides": {
        "msg": (
            "Prepárame una presentación de PowerPoint de 8 diapositivas sobre el ciclo "
            "del agua para alumnos de 3º de primaria: títulos claros, poco texto por "
            "diapositiva y una actividad final para hacer en clase. Guárdala como "
            "/a0/usr/workdir/ciclo-agua.pptx y dime qué has puesto en cada diapositiva."
        ),
        "files": ["/a0/usr/workdir/ciclo-agua.pptx"],
    },
    "research": {
        "msg": (
            "Investiga qué dice la normativa estatal vigente (LOMLOE y su real decreto de "
            "enseñanzas mínimas de Primaria) sobre la evaluación en Educación Primaria: "
            "cómo se evalúa, cómo se expresan las calificaciones y cuándo se repite curso. "
            "Dame un resumen de una página en español y enlaza las fuentes oficiales que "
            "hayas consultado (BOE u otras)."
        ),
        "files": [],
    },
    "worksheet": {
        "msg": (
            "Hazme una ficha de ejercicios de fracciones para 5º de primaria: 10 ejercicios "
            "de dificultad creciente y el solucionario en una página aparte. Guárdala en "
            "Word como /a0/usr/workdir/fracciones-5.docx."
        ),
        "files": ["/a0/usr/workdir/fracciones-5.docx"],
    },
    "memory": {
        # Two chats: the fact goes in the first, the question in a fresh one.
        "msg": (
            "Para que lo tengas en cuenta siempre: soy maestra de 3º de primaria en un "
            "colegio público de Galicia y prefiero las diapositivas con muy poco texto y "
            "muchas imágenes. Guárdalo en tu memoria."
        ),
        "followup": "¿Qué sabes de mí y de cómo me gustan las presentaciones?",
        "files": [],
    },
}


def kube(*args, check=True):
    return subprocess.run(
        ["kubectl", "--context", CTX, "-n", NS, *args],
        check=check, capture_output=True, text=True,
    ).stdout


def pod_exec(script):
    return kube("exec", "deploy/agent-zero", "-c", "agent-zero", "--", "bash", "-lc", script)


def api_token():
    out = pod_exec(
        "cd /a0 && /opt/venv-a0/bin/python -c "
        "'from helpers.settings import get_settings; print(get_settings()[\"mcp_server_token\"])' "
        "2>/dev/null | tail -1"
    )
    return out.strip()


def gateway_status():
    out = kube("exec", "deploy/agent-zero", "-c", "agent-zero", "--", "curl", "-s", "-m", "5",
               "http://strata.strata.svc.cluster.local:8080/gateway/status", check=False)
    try:
        return json.loads(out)["served"]
    except Exception:
        return None


def send(token, message, context_id=""):
    body = json.dumps({"message": message, "context_id": context_id}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/api/api_message", data=body, method="POST",
        headers={"Content-Type": "application/json", "X-API-KEY": token},
    )
    with urllib.request.urlopen(req, timeout=3600) as r:
        return json.loads(r.read())


def file_info(paths):
    info = {}
    for p in paths:
        out = pod_exec(f"stat -c %s {p} 2>/dev/null || echo MISSING").strip()
        info[p] = out
    return info


def main():
    names = sys.argv[1:] or list(TASKS)
    outdir = os.path.join(HERE, "out", time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(outdir)
    pf = subprocess.Popen(
        ["kubectl", "--context", CTX, "-n", NS, "port-forward", "svc/agent-zero", f"{PORT}:80"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(3)
        token = api_token()
        if not token:
            sys.exit("no API token from the pod")
        for name in names:
            t = TASKS[name]
            rec = {"task": name, "message": t["msg"], "served_before": gateway_status()}
            t0 = time.time()
            try:
                r = send(token, t["msg"])
                rec["response"] = r.get("response")
                rec["context_id"] = r.get("context_id")
                if "followup" in t:
                    r2 = send(token, t["followup"])  # fresh chat on purpose
                    rec["followup"] = t["followup"]
                    rec["followup_response"] = r2.get("response")
            except Exception as e:
                rec["error"] = repr(e)
            rec["seconds"] = round(time.time() - t0)
            rec["served_after"] = gateway_status()
            rec["files"] = file_info(t["files"])
            with open(os.path.join(outdir, f"{name}.json"), "w") as f:
                json.dump(rec, f, ensure_ascii=False, indent=2)
            print(f"{name}: {rec['seconds']} s, files={rec['files']}, "
                  f"error={rec.get('error')}", flush=True)
    finally:
        pf.terminate()
    print(outdir)


if __name__ == "__main__":
    main()
