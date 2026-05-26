#!/usr/bin/env python3
"""Tdarr AV1 quality gate.

Polls Tdarr's FileJSONDB for successful h264 → AV1 transcodes that
haven't been validated yet, runs a 3-window libvmaf check on each, and
on pass replaces the source with the AV1 output. On fail, leaves both
files in place and logs the failure for manual review.

Runs as a k8s CronJob. Sequential processing — one file at a time —
because libvmaf and the running tdarr-node pods already saturate the
host CPU. Set MAX_FILES to bound the run length per invocation.

Pass criterion:
  All three sampled windows (10% / 50% / 90% of the file) must score
  VMAF >= VMAF_MIN. Worst-window gating, not mean, because per-window
  outliers are the kind of localized regression we care about catching.

State:
  Stateless across runs. "Already validated" is inferred by the source
  file no longer existing at the path Tdarr's FileJSONDB row points to
  (because a previous pass moved the AV1 over it). Failed files keep
  both source + output and reappear in subsequent runs — they will be
  re-checked unless added to the explicit skip list.

See ~/Documents/knowledge/projects/tdarr.md "Quality gating" for the
larger design context.
"""
import glob
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.request

TDARR_URL = os.environ["TDARR_URL"]
TDARR_API_KEY = os.environ["TDARR_API_KEY"]
LIBRARY_DB = os.environ.get("LIBRARY_DB", "anime_health")
SOURCE_PREFIX = os.environ.get("SOURCE_PREFIX", "/media/tv/")
OUTPUT_PREFIX = os.environ.get("OUTPUT_PREFIX", "/media/tv-pilot/")
AUDIT_DIR = os.environ.get("AUDIT_DIR", "/media/tdarr-validator")
VMAF_MIN = float(os.environ.get("VMAF_MIN", "95.0"))
WINDOW_SECONDS = int(os.environ.get("WINDOW_SECONDS", "30"))
MAX_FILES = int(os.environ.get("MAX_FILES", "10"))
MAX_RUNTIME_SECONDS = int(os.environ.get("MAX_RUNTIME_SECONDS", "21600"))  # 6h
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")
# Sharding: pod N of SHARD_COUNT processes files where sha1(src) % SHARD_COUNT == SHARD_INDEX.
# Disjoint sets across pods, stable across restarts, no coordinator needed.
SHARD_INDEX = int(os.environ.get("SHARD_INDEX", "0"))
SHARD_COUNT = int(os.environ.get("SHARD_COUNT", "1"))
# When >0, run forever: each iteration re-lists candidates and processes a batch
# bounded by MAX_FILES + MAX_RUNTIME_SECONDS, then sleeps LOOP_SECONDS.
LOOP_SECONDS = int(os.environ.get("LOOP_SECONDS", "0"))

VMAF_RE = re.compile(r"VMAF score:\s*([0-9.]+)")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("validator")


def tdarr_cruddb(payload: dict) -> dict | list:
    req = urllib.request.Request(
        f"{TDARR_URL}/api/v2/cruddb",
        data=json.dumps({"data": payload}).encode(),
        headers={"x-api-key": TDARR_API_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def fingerprint(path: str) -> dict:
    s = os.stat(path)
    # mtime rounded to int — NFS clients can disagree on the sub-second.
    return {"mtime": int(s.st_mtime), "size": s.st_size}


def load_passed_index() -> dict[str, dict]:
    """Build {src_path: {src_fp, out_fp}} from every passed-*.jsonl audit
    file, so prior validations can be skipped when nothing on disk has
    changed. Only "pass" records count — failures/errors are re-checked."""
    index: dict[str, dict] = {}
    for path in sorted(glob.glob(f"{AUDIT_DIR}/passed-*.jsonl")):
        with open(path) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("verdict") != "pass":
                    continue
                src = rec.get("src")
                fp = rec.get("fingerprint")
                if src and fp:
                    index[src] = fp
    return index


def in_shard(src: str) -> bool:
    if SHARD_COUNT <= 1:
        return True
    h = int(hashlib.sha1(src.encode()).hexdigest(), 16)
    return h % SHARD_COUNT == SHARD_INDEX


def candidates():
    rows = tdarr_cruddb({"collection": "FileJSONDB", "mode": "getAll"})
    for r in rows:
        if r.get("DB") != LIBRARY_DB:
            continue
        if r.get("TranscodeDecisionMaker") != "Transcode success":
            continue
        src = r.get("file")
        if not src or not src.startswith(SOURCE_PREFIX):
            continue
        if not in_shard(src):
            continue
        out = OUTPUT_PREFIX + src[len(SOURCE_PREFIX):]
        if not os.path.isfile(src):
            # Already replaced by a previous validator run.
            continue
        if not os.path.isfile(out):
            # Output missing — leave for the next library scan to reconcile.
            continue
        yield {"src": src, "out": out, "tdarr_id": r.get("_id")}


def probe_duration(path: str) -> float | None:
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def vmaf_window(src: str, out: str, start: float) -> tuple[float | None, str]:
    # Input seek (-ss/-t BEFORE each -i): jumps to the keyframe near `start`
    # and decodes only the window. Output seek (-ss AFTER -i) instead decodes
    # and discards from t=0, which makes both memory and wall time scale with
    # the seek offset — a 90% window on a 24-min file then buffers tens of GB
    # and OOMs. Input seek is accurate in modern ffmpeg (it decodes from the
    # keyframe to the exact seek point) and bounded. See
    # architecture/libvmaf-measurement-gotchas in the vault.
    cmd = [
        FFMPEG, "-nostdin", "-hide_banner",
        "-ss", f"{start:.3f}", "-t", str(WINDOW_SECONDS), "-i", out,
        "-ss", f"{start:.3f}", "-t", str(WINDOW_SECONDS), "-i", src,
        "-lavfi",
        "[0:v]format=yuv420p10le[dist];[1:v]format=yuv420p10le[ref];"
        "[dist][ref]libvmaf=pool=mean",
        "-f", "null", "-",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    m = VMAF_RE.search(r.stderr)
    return (float(m.group(1)) if m else None, r.stderr[-1500:])


def validate(pair: dict) -> dict:
    src, out = pair["src"], pair["out"]
    rec = {"src": src, "out": out, "tdarr_id": pair["tdarr_id"]}
    duration = probe_duration(src)
    if duration is None or duration < 3 * WINDOW_SECONDS + 10:
        rec["verdict"] = "error_duration"
        rec["duration"] = duration
        return rec
    starts = [duration * 0.10, duration * 0.50, duration * 0.90]
    window_results = []
    for s in starts:
        v, err = vmaf_window(src, out, s)
        window_results.append({"start": round(s, 2), "vmaf": v, "err": err if v is None else None})
    rec["windows"] = [{"start": w["start"], "vmaf": w["vmaf"]} for w in window_results]
    if any(w["vmaf"] is None for w in window_results):
        rec["verdict"] = "error_vmaf"
        rec["error_tails"] = [w["err"] for w in window_results if w["err"]][:1]
        return rec
    vmafs = [w["vmaf"] for w in window_results]
    rec["vmaf_mean"] = round(sum(vmafs) / len(vmafs), 3)
    rec["vmaf_worst"] = round(min(vmafs), 3)
    rec["verdict"] = "pass" if rec["vmaf_worst"] >= VMAF_MIN else "fail"
    return rec


def replace_source(pair: dict) -> None:
    # On the same NFS filesystem, os.replace is atomic and overwrites src.
    os.replace(pair["out"], pair["src"])


def append_audit(name: str, rec: dict) -> None:
    # Per-shard filename so concurrent pods don't interleave NFS appends.
    # load_passed_index() globs passed-*.jsonl so it still sees every shard.
    os.makedirs(AUDIT_DIR, exist_ok=True)
    suffix = f"-s{SHARD_INDEX}" if SHARD_COUNT > 1 else ""
    path = f"{AUDIT_DIR}/{name}-{time.strftime('%Y%m%d')}{suffix}.jsonl"
    with open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")


def run_once() -> dict:
    started = time.time()
    counts = {"pass": 0, "pass_replayed": 0, "fail": 0,
              "error_vmaf": 0, "error_duration": 0, "skipped_dry": 0}
    passed_index = load_passed_index()
    log.info("loaded passed_index entries=%d", len(passed_index))
    pool = list(candidates())
    log.info("candidates available=%d", len(pool))
    processed = 0
    for pair in pool:
        if processed >= MAX_FILES:
            log.info("max_files reached, stopping")
            break
        if time.time() - started >= MAX_RUNTIME_SECONDS:
            log.info("max_runtime reached, stopping")
            break
        prior = passed_index.get(pair["src"])
        if prior:
            current_fp = {"src": fingerprint(pair["src"]), "out": fingerprint(pair["out"])}
            if prior == current_fp:
                if DRY_RUN:
                    counts["skipped_dry"] += 1
                    log.info("skipping %s (prior pass, dry_run)", pair["src"])
                    continue
                # Real mode: trust the prior pass — perform the move without
                # recomputing VMAF. Move first, audit second, so a failed
                # os.replace doesn't leave an orphaned "passed" record.
                replace_source(pair)
                append_audit("passed", {
                    "src": pair["src"], "out": pair["out"],
                    "tdarr_id": pair["tdarr_id"],
                    "verdict": "pass", "replayed_from_prior": True,
                    "fingerprint": current_fp, "dry_run": False,
                })
                counts["pass_replayed"] += 1
                processed += 1
                log.info("replayed prior pass, replaced source: %s", pair["src"])
                continue
            log.info("fingerprint changed since prior pass, re-validating %s", pair["src"])
        log.info("validating %s", pair["src"])
        t0 = time.time()
        rec = validate(pair)
        rec["seconds"] = round(time.time() - t0, 1)
        rec["dry_run"] = DRY_RUN
        if rec["verdict"] == "pass":
            rec["fingerprint"] = {
                "src": fingerprint(pair["src"]),
                "out": fingerprint(pair["out"]),
            }
        counts[rec["verdict"]] = counts.get(rec["verdict"], 0) + 1
        log.info(
            "verdict=%s vmaf_mean=%s vmaf_worst=%s seconds=%.1f",
            rec["verdict"], rec.get("vmaf_mean"), rec.get("vmaf_worst"), rec["seconds"],
        )
        if rec["verdict"] == "pass":
            append_audit("passed", rec)
            if not DRY_RUN:
                replace_source(pair)
                log.info("replaced source with AV1: %s", pair["src"])
        elif rec["verdict"] == "fail":
            append_audit("failed", rec)
        else:
            append_audit("errors", rec)
        processed += 1
    log.info("done processed=%d counts=%s elapsed=%.1fs",
             processed, counts, time.time() - started)
    return counts


def main() -> int:
    log.info(
        "starting validator dry_run=%s max_files=%d vmaf_min=%.1f "
        "shard=%d/%d loop_seconds=%d",
        DRY_RUN, MAX_FILES, VMAF_MIN, SHARD_INDEX, SHARD_COUNT, LOOP_SECONDS,
    )
    if LOOP_SECONDS <= 0:
        run_once()
        return 0
    while True:
        run_once()
        log.info("iteration complete, sleeping %ds", LOOP_SECONDS)
        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
