# Tdarr next steps

Pilot validated end-to-end on 2026-04-29 (S01E01 of "Cells at Work!" → AV1 444 MB,
output to /media/tv-pilot/Season 1/, source preserved). Recipe matches the bench
REPORT (libsvtav1 preset 4 crf 26 + Opus channel-aware bitrate, VMAF 97.4).

## 1. Scaling the pilot

Library `pilot_av1_001` is currently scoped to `/media/tv/Cells at Work!`.
Bump `folder` to `/media/tv` (or wherever) when ready for the full library
sweep. The DaemonSet workers handle parallelism (currently 12 transcodecpu +
1 healthcheckcpu per pod × 2 pods).

Worker tuning note: 12 simultaneous libsvtav1 encodes per pod compete on a
12-core cgroup limit. Each ffmpeg gets ~1 core. libsvtav1's sweet spot is
4-6 threads/file, so `2 workers × 6 threads` is more efficient throughput
than `12 workers × 1 thread`. To set thread count per encode: extend the
plugin's preset with `-threads N` (libsvtav1 also honours `-svtav1-params lp=N`).
Measure first.

## 2. Healthcheck-driven corruption detection (anime_health library)

A second library `anime_health` is set up scoped to `/media/tv` with
`processHealthChecks: true, processTranscodes: false`. It will run Tdarr's
built-in `ffmpeg -err_detect explode` decode pass on every video file and
update the `HealthCheck` field in `FileJSONDB`:

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

## 3. Quality gating (two-tier: SSIM now, VMAF later)

The bench measured VMAF 97.4 globally, but per-file regressions are
possible (rare scenes the encoder handles poorly, corrupt source).
Originals stay (`folderToFolderConversionDeleteSource: false`) so a
bad transcode can never destroy data. Two checks, in order:

### Tier 1 — inline SSIM (live as of 2026-04-30)

`Tdarr_Plugin_anime_av1_ssim_gate.js` (in this directory, uploaded to
Tdarr as Local plugin `Tdarr_Plugin_anime_av1_ssim_gate`, attached as
plugin2 on `pilot_av1_001` with `minSsim=0.95, deleteOnFail=true`).

- `Stage: 'Post-processing'` → runs in `runPostProcPlugins` after
  `cacheCopyService` has already moved the file to the output folder.
- Computes `ssim(source, transcoded)` via the bundled ffmpeg's
  `[0:v:0][1:v:0]ssim=stats_file=...` filter; parses the `All:` score
  from ffmpeg's stderr.
- If score < `minSsim`, `unlink`s the moved output. Source untouched.
- Tdarr's runPostProcPlugins doesn't have a rollback hook, so the
  `FileJSONDB` row will still say `TranscodeDecisionMaker = 'Transcode
  success'` until a rescan. anisub's reconcile phase (or a manual
  rescan) will pick up that the output is missing and re-queue.

**Why SSIM and not VMAF for this tier**: bundled Tdarr_Node ffmpeg is
the Jellyfin build (verified on the running pod via `ffmpeg -version`
flags) — `--enable-libsvtav1 --enable-libx265 --enable-ffnvcodec ...`
but **no `--enable-libvmaf`**. It ships `ssim`, `psnr`, `xpsnr`, and
`vmafmotion`. SSIM ≥ 0.95 catches catastrophic regressions ("did
something break") with no false negatives. Real libvmaf would require
a custom Tdarr_Node image or a sidecar with ffmpeg-full at a second
path the plugin can shell out to — both add image-maintenance cost
that the SSIM gate avoids.

### Tier 2 — final libvmaf acceptance pass (planned)

Before flipping `folderToFolderConversionDeleteSource: true` (i.e.,
before reclaiming the projected ~4.57 TB of source files), every
SSIM-passed AV1 should be re-validated against its source with
**real libvmaf** — bench-grade per-frame quality scoring. Only files
that pass both tiers should have their source deleted. Likely shape:

- External script (Go/Python) on a host with a libvmaf-capable
  ffmpeg (the bench used nixpkgs `ffmpeg-full` 8.0.1).
- Reads candidates from Tdarr's `FileJSONDB` where
  `TranscodeDecisionMaker = 'Transcode success'` and `DB =
  pilot_av1_001` (or whatever AV1 library handles the rollout).
- For each pair (source path from `originalLibraryFile.file` if still
  recorded, else derived from the library's folder mapping; output
  path from the moved-to location), run
  `ffmpeg -i SOURCE -i AV1 -lavfi libvmaf -f null -` and parse the
  harmonic-mean VMAF.
- On VMAF ≥ threshold (likely 95 or 96, given the bench median 97.4):
  `rm` the source. On fail: log, leave both, surface for review.

Defer this until: (a) the AV1 sweep has produced a sizeable batch
of SSIM-passed outputs to validate, AND (b) the disk-space pressure
from keeping originals + AV1s justifies the cleanup work.

### Where to fail safely

For the inline SSIM gate, "fail safely" is automatic — the plugin
deletes only the output, source is preserved by the library setting.
For the final VMAF pass, the script SHOULD never delete a source
without a green VMAF score AND a sanity check that the AV1 file is
present and non-empty.

## 4. Manual queries / state

- API key: `tdarr` k8s secret (key `seeded-api-key`)
- All cruddb collections accessible via POST `/api/v2/cruddb`:
  `{data:{collection:..., mode:..., docID:..., obj:...}}`
  - modes: `getAll`, `getById`, `insert`, `update`, `removeOne`
- Verbose logging on the server: `globalsettings.verboseLogs` in
  `SettingsGlobalJSONDB`
