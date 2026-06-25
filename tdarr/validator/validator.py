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
# libvmaf's internal worker thread count. Bigger = faster decode of the
# 24-min-ish anime files. 4 is a reasonable default vs the validator pod's
# cpu limit of 4; bump alongside `resources.limits.cpu` if it changes.
VMAF_THREADS = int(os.environ.get("VMAF_THREADS", "4"))
MAX_FILES = int(os.environ.get("MAX_FILES", "10"))
MAX_RUNTIME_SECONDS = int(os.environ.get("MAX_RUNTIME_SECONDS", "21600"))  # 6h
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")
# Sharding: pod N of SHARD_COUNT processes files where sha1(SHARD_SALT+src) % SHARD_COUNT == SHARD_INDEX.
# Disjoint sets across pods, stable across restarts, no coordinator needed.
# SHARD_SALT bumps the bucket mapping when distribution becomes uneven — set to
# a fresh value across all shards to redistribute the unprocessed queue. The
# pass/fail indexes glob across all shards (passed-*.jsonl), so files that were
# already validated under the old salt remain skipped after the salt change.
SHARD_INDEX = int(os.environ.get("SHARD_INDEX", "0"))
SHARD_COUNT = int(os.environ.get("SHARD_COUNT", "1"))
SHARD_SALT = os.environ.get("SHARD_SALT", "")
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


def _load_verdict_index(glob_pattern: str, want_verdict: str) -> dict[str, dict]:
    """Generic loader: scan jsonl audit files and build a
    {src_path: fingerprint} index for records matching `want_verdict`
    under the CURRENT algorithm."""
    index: dict[str, dict] = {}
    for path in sorted(glob.glob(f"{AUDIT_DIR}/{glob_pattern}")):
        with open(path) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("verdict") != want_verdict:
                    continue
                if rec.get("algorithm") != ALGORITHM:
                    continue
                src = rec.get("src")
                fp = rec.get("fingerprint")
                if src and fp:
                    index[src] = fp
    return index


def load_passed_index() -> dict[str, dict]:
    """{src_path: fingerprint} of files that already passed under the
    current algorithm. The validator skips these unless DRY_RUN is off
    and the prior pass needs replay."""
    return _load_verdict_index("passed-*.jsonl", "pass")


def load_failed_index() -> dict[str, dict]:
    """{src_path: fingerprint} of files that already failed under the
    current algorithm. These are skipped silently on subsequent runs
    until their on-disk state changes — without this, a handful of
    known-bad transcodes (e.g. Black Clover S1, encoder regression)
    eat most of the cluster CPU re-failing the same files every loop
    iteration."""
    return _load_verdict_index("failed-*.jsonl", "fail")


def load_errored_index() -> dict[str, dict]:
    """{src_path: fingerprint} of files that errored on the duration
    gate (src shorter than 30s — samples/extras tdarr transcoded but
    that VMAF can't meaningfully score). Duration is a stable property
    of the file, so this is terminal: skip on subsequent runs until the
    file changes on disk. Without this the short files never enter
    passed/failed indexes and get re-listed + re-probed every loop
    forever, pinning the candidate count and burning CPU. Only
    error_duration is cached — error_vmaf can be transient (resource
    pressure) and is left to retry."""
    return _load_verdict_index("errors-*.jsonl", "error_duration")


def in_shard(src: str) -> bool:
    if SHARD_COUNT <= 1:
        return True
    h = int(hashlib.sha1((SHARD_SALT + src).encode()).hexdigest(), 16)
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


ALGORITHM = "full_v2"
# Whole-file VMAF, no input seek. The previous windowed sampler
# (algorithm "windowed_v1") seeked to t=10/50/90% independently on src
# and out, which produced garbage scores when libsvtav1 dropped duplicate
# frames during encode (VFR-ish sources: e.g. Black Clover S1 reported
# 34377 src frames vs 34138 AV1 frames over an identical 1435s duration
# — seeks to a wall-clock T landed on different source moments on each
# side, scoring 49-66 on visually-fine encodes). The fix is to feed both
# files start-to-end and let libvmaf's framesync align by PTS: each
# AV1 frame is compared against the h264 frame at the same PTS, and
# duplicate-frame drops show up as the (correct, lossless) cost they
# actually are. Trade-off: ~10x slower per file vs windowed sampling,
# which the user explicitly accepted in exchange for correctness.

def vmaf_full(src: str, out: str) -> tuple[float | None, str]:
    cmd = [
        FFMPEG, "-nostdin", "-hide_banner",
        "-i", out,
        "-i", src,
        "-an", "-sn",   # ignore audio + subtitles, decode video only
        "-lavfi",
        "[0:v]format=yuv420p10le[dist];"
        "[1:v]format=yuv420p10le[ref];"
        f"[dist][ref]libvmaf=pool=mean:n_threads={VMAF_THREADS}",
        "-f", "null", "-",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    m = VMAF_RE.search(r.stderr)
    return (float(m.group(1)) if m else None, r.stderr[-1500:])


def validate(pair: dict) -> dict:
    src, out = pair["src"], pair["out"]
    rec = {"src": src, "out": out, "tdarr_id": pair["tdarr_id"],
           "algorithm": ALGORITHM}
    duration = probe_duration(src)
    if duration is None or duration < 30:
        rec["verdict"] = "error_duration"
        rec["duration"] = duration
        return rec
    v, err = vmaf_full(src, out)
    if v is None:
        rec["verdict"] = "error_vmaf"
        rec["error_tail"] = err
        return rec
    rec["vmaf"] = round(v, 3)
    rec["verdict"] = "pass" if v >= VMAF_MIN else "fail"
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
    counts = {"pass": 0, "pass_replayed": 0, "fail": 0, "fail_cached": 0,
              "error_vmaf": 0, "error_duration": 0, "error_duration_cached": 0,
              "skipped_dry": 0}
    passed_index = load_passed_index()
    failed_index = load_failed_index()
    errored_index = load_errored_index()
    log.info("loaded passed_index entries=%d failed_index entries=%d errored_index entries=%d",
             len(passed_index), len(failed_index), len(errored_index))
    try:
        pool = list(candidates())
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        # Tdarr server unreachable (pod rescheduling, node down, etc.).
        # Don't crash — keep the pod Running so it picks up automatically
        # when the API returns. The outer loop sleeps LOOP_SECONDS and
        # retries. Previous behavior crashlooped, accumulating 150+
        # restarts during a 12h orange-pi5 outage.
        log.warning("tdarr API unreachable, skipping iteration: %s", e)
        return counts
    log.info("candidates available=%d", len(pool))
    processed = 0
    for pair in pool:
        if processed >= MAX_FILES:
            log.info("max_files reached, stopping")
            break
        if time.time() - started >= MAX_RUNTIME_SECONDS:
            log.info("max_runtime reached, stopping")
            break
        prior_fail = failed_index.get(pair["src"])
        if prior_fail:
            current_fp = {"src": fingerprint(pair["src"]), "out": fingerprint(pair["out"])}
            if prior_fail == current_fp:
                # Same files on disk as last time this failed: skip
                # silently. If a fresh transcode lands (different mtime
                # or size on `out`), the fingerprint mismatches and we
                # re-validate.
                counts["fail_cached"] += 1
                continue
            log.info("fingerprint changed since prior fail, re-validating %s", pair["src"])
        prior_err = errored_index.get(pair["src"])
        if prior_err:
            current_fp = {"src": fingerprint(pair["src"]), "out": fingerprint(pair["out"])}
            if prior_err == current_fp:
                # Same short file as last time: skip silently. A re-transcode
                # (different out fingerprint) re-validates.
                counts["error_duration_cached"] += 1
                continue
            log.info("fingerprint changed since prior error_duration, re-validating %s", pair["src"])
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
        if rec["verdict"] in ("pass", "fail", "error_duration"):
            rec["fingerprint"] = {
                "src": fingerprint(pair["src"]),
                "out": fingerprint(pair["out"]),
            }
        counts[rec["verdict"]] = counts.get(rec["verdict"], 0) + 1
        log.info(
            "verdict=%s vmaf=%s seconds=%.1f",
            rec["verdict"], rec.get("vmaf"), rec["seconds"],
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
