# Tdarr next steps

Pilot validated end-to-end on 2026-04-29 (S01E01 of "Cells at Work!" → AV1 444 MB,
output to /media/tv-pilot/Season 1/, source preserved). Recipe matches the bench
REPORT (libsvtav1 preset 4 crf 26 + Opus channel-aware bitrate, VMAF 97.4).

## 1. Scaling out: promote `anime_health` to the unified library

The two libraries (`pilot_av1_001` for AV1 transcode, `anime_health` for
healthchecks) collapse into one for the rollout. **Edit `anime_health`**
(don't bump `pilot_av1_001`'s folder) — `anime_health` is already scoped
to `/media/tv` and has thousands of files with `HealthCheck` state
populated; pilot has six. Editing pilot would mean a fresh scan of the
whole tree and losing in-progress healthcheck progress.

### When to flip

Hold until the current `anime_health` healthcheck sweep is mostly drained.
Quick check via the API:

```bash
API_KEY=$(kubectl --context lamg -n piracy get secret tdarr -o jsonpath='{.data.seeded-api-key}' | base64 -d)
kubectl --context lamg -n piracy exec deploy/tdarr-deployment -c tdarr -- \
  curl -sS -X POST http://localhost:8266/api/v2/cruddb \
  -H "x-api-key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"data":{"collection":"FileJSONDB","mode":"getAll"}}' \
  | jq '[.[] | select(.DB=="anime_health" and .HealthCheck=="Queued")] | length'
```

When that drops near zero, ramp. Otherwise the queue builder is racing
both passes on every file and the server pod has more reason to thrash.

### What to change

On `anime_health` (via the cruddb API or the UI):

- `name: "Anime archive"` — the display label; `_id` stays
  `anime_health` (it's referenced by every `FileJSONDB.DB` row).
- `processTranscodes: true`
- `output: /media/tv-pilot` (or rename — `/media/tv-av1` is more honest now)
- `folderToFolderConversion: true`
- `folderToFolderConversionDeleteSource: false`  ← keep originals; the
  Tier 2 batch SSIM+VMAF script (§3) is the only thing that should ever
  delete a source.
- `pluginIDs: [{_id:"plugin1", id:"Tdarr_Plugin_anime_av1_pilot",
  checked:true, source:"Local", priority:0, InputsDB:{}}]`
- `decisionMaker.settingsPlugin: true` ← **required**, otherwise the
  worker takes the "no settings" branch in `determineTranscodeSettings`
  and marks every file `Transcode error` without invoking ffmpeg.
  Verified failure mode 2026-05-01: 505/6609 files errored before the
  flag was flipped (then reset to `Queued` and retried). The pluginIDs
  list alone is not enough; this toggle picks the plugin path over
  basic-video / basic-audio / none.

The plugin already skips non-h264 sources (HEVC and AV1 fall through
with `Primary video codec is X; only h264 sources are converted`),
so no extra codec filter is needed.

### Cleanup

Delete `pilot_av1_001` once `anime_health` is producing transcodes —
keeping it around just double-scans `/media/tv` for one show.

The pilot's one finished output (`/media/tv-pilot/Season 1/Cells at
Work! - S01E01...mkv`, 465 MB) will be re-encoded — its
`F2FOutputJSONDB` row is keyed to `pilot_av1_001`, so `anime_health`'s
queue builder won't see it and will re-queue S01E01. Cheaper to let
the one episode re-run than to hand-insert a parallel F2F row.

### Worker tuning note

12 simultaneous libsvtav1 encodes per pod compete on a 12-core cgroup
limit. Each ffmpeg gets ~1 core. libsvtav1's sweet spot is 4-6
threads/file, so `2 workers × 6 threads` is more efficient throughput
than `12 workers × 1 thread`. To set thread count per encode: extend
the plugin's preset with `-threads N` (libsvtav1 also honours
`-svtav1-params lp=N`). Measure first.

## 2. Healthcheck-driven corruption detection (anime_health library)

`anime_health` is scoped to `/media/tv`. Initially set up with
`processHealthChecks: true, processTranscodes: false`; once §1's
migration lands it carries both flags. The healthcheck pass is what
matters here: Tdarr runs its built-in `ffmpeg -err_detect explode`
decode pass on every video file and updates the `HealthCheck` field
in `FileJSONDB`:

- `Queued` — scan pending
- `Success` — pass (verified against the running 2.70.01 DB on 2026-04-30)
- `Unhealthy` — decode errors (corrupt source — the cruncharr wedge-bug
  class flagged in `vault/projects/tdarr.md`)

### Bridge to anisub

anisub's job is to flag corrupt files for re-download (vault note:
"downstream bridge script (handoff from current corrupt-recovery work)").
Recommended consumer query, since both server and anisub talk to the
Tdarr API:

```python
import json, urllib.request
req = urllib.request.Request(
    'http://tdarr:8266/api/v2/cruddb',
    data=json.dumps({'data':{'collection':'FileJSONDB','mode':'getAll'}}).encode(),
    headers={'x-api-key': SEEDED_API_KEY, 'Content-Type':'application/json'},
    method='POST',
)
files = json.loads(urllib.request.urlopen(req).read().decode())
unhealthy = [f for f in files
             if f.get('HealthCheck') == 'Unhealthy'
             and f.get('DB') == 'anime_health']
# Each f['_id'] is the absolute path. Use anisub's existing path → Sonarr
# episodeId resolver (Phase 1 maintains this mapping), then DELETE
# /api/v3/episodefile/{id} + EpisodeSearch to redownload (pattern from
# runbooks/sonarr-bulk-redownload.md).
```

Tdarr API URL inside the cluster: `http://tdarr.piracy.svc.cluster.local:8266`
(or the `tdarr` service name from the same namespace).

## 3. Quality gating (single batch pass: SSIM + VMAF before source deletion)

The bench measured VMAF 97.4 globally, but per-file regressions are
possible (rare scenes the encoder handles poorly, corrupt source).
Originals stay (`folderToFolderConversionDeleteSource: false`) so a
bad transcode can never destroy data — that safety net is what makes
the single-pass gating below safe.

### Why no inline SSIM gate (rejected 2026-04-30)

The earlier plan was an inline SSIM check via a post-processing
plugin (`Tdarr_Plugin_anime_av1_ssim_gate.js`, kept under
`../plugins/` for reuse). Tdarr loads post-processing plugins synchronously inside
the server's Node event loop and waits for the plugin function to
return. The plugin shells out to `ffmpeg ssim` which runs for
minutes per file, blocking the event loop the whole time. Kubelet's
liveness probe at `/:8265` then times out and kills the pod
mid-run, the verdict is lost, the file's `TranscodeDecisionMaker`
stays `Queued`, and the transcode is re-queued. Verified failure
mode on 2026-04-30: pod restart count climbed from 0 to 12 in
~80 min. The relaxed liveness probe in `tdarr.yml` (60s/60s/30s/5)
addresses the kubelet side but doesn't fix the throughput hit —
a single SSIM run still freezes all 24 transcode workers' next
poll for several minutes.

The plugin file is kept for reference: its SSIM-filter parsing and
`otherArguments.originalLibraryFile` contract are reusable in the
batch pass below.

### Tier 2 — continuous VMAF acceptance pass (deployed 2026-05-26)

Tdarr keeps `folderToFolderConversionDeleteSource: false`
permanently. The deletion decision is owned by `tdarr-validator`,
which now runs as **three Deployments** (`tdarr-validator-{0,1,2}`),
one pinned to each `workload=media` node:

- Code: `tdarr/validator/validator.py`. Per file: 3 × 30s libvmaf
  windows at 10/50/90% of duration, pass = worst window ≥ 95
  (env `VMAF_MIN`). On pass + `DRY_RUN=false`, atomic
  `os.replace(out, src)` on the shared NFS mount.
- Manifest: `deployments/piracy/tdarr-validator.yml`.
- Sharding: pod N processes files where
  `sha1(src) % SHARD_COUNT == SHARD_INDEX`. Disjoint sets, no
  coordinator. Each pod loops forever (`LOOP_SECONDS=30`),
  re-listing candidates every iteration, bounded per-iteration by
  `MAX_FILES=500` + `MAX_RUNTIME_SECONDS=3600`.
- Audit logs at `/media/tdarr-validator/{passed,failed,errors}-YYYYMMDD-s{0,1,2}.jsonl`.
  Sharded filenames avoid concurrent NFS appends.
  `load_passed_index()` globs `passed-*.jsonl` so every pod still
  sees every other shard's fingerprints (fingerprint-stable
  skipping across shards).
- The earlier `tdarr-validator` CronJob is deleted; the only
  scheduling now is the Deployment loop.

### Flipping DRY_RUN → false

After spot-checking a per-shard `passed-YYYYMMDD-sN.jsonl` and
confirming the VMAF scores look right:

```bash
for n in 0 1 2; do
  kubectl --context lamg -n piracy set env deploy/tdarr-validator-$n DRY_RUN=false
done
```

The replay path on fingerprint hits then performs the move without
recomputing VMAF (`pass_replayed` counter in logs), so the prior
DRY_RUN cache front-loads the first real run.

### Where to fail safely

The validator SHOULD never delete a source without:
1. Per-window VMAF ≥ VMAF_MIN on all three windows
2. Sanity check that the AV1 file is present and non-empty
3. Sanity check that the AV1 stream count matches the source's
   non-attachment streams (catches truncated muxes)

(2) and (3) are not yet enforced — open work.

## 4. Manual queries / state

- API key: `tdarr` k8s secret (key `seeded-api-key`)
- All cruddb collections accessible via POST `/api/v2/cruddb`:
  `{data:{collection:..., mode:..., docID:..., obj:...}}`
  - modes: `getAll`, `getById`, `insert`, `update`, `removeOne`
- Verbose logging on the server: `globalsettings.verboseLogs` in
  `SettingsGlobalJSONDB`
