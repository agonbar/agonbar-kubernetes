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
- `Healthy` — pass
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

## 3. VMAF quality validation (TODO)

The bench measured VMAF 97.4 globally, but per-file regressions are
possible (rare scenes the encoder handles poorly). To validate each
transcode:

### Option A: post-processing plugin
Tdarr supports `Stage: 'Post-processing'` plugins that run after the
cache file is created but before cacheCopyService moves it. Plugin can
shell out to `ffmpeg -i ORIGINAL -i CACHE -lavfi libvmaf -f null -` and
parse the score. If below threshold (e.g. 92), the plugin fails the job
and the cache file is discarded.

Skeleton:
```js
const details = () => ({
  id: 'Tdarr_Plugin_anime_av1_vmaf_check',
  Stage: 'Post-processing',
  Name: 'AV1 VMAF gate',
  Type: 'Video',
  Operation: 'Other',
  Description: 'Compute VMAF original vs transcoded; reject if score < 92.',
  Inputs: [
    {name: 'minVmaf', type: 'number', defaultValue: 92, ...},
  ],
});
const plugin = (file, librarySettings, inputs, otherArguments) => {
  // file.file = the cache (transcoded) file path
  // otherArguments has the original path
  // Run ffmpeg with libvmaf, parse score, fail if below threshold.
  ...
};
```

### Option B: external script triggered via Tdarr webhook
Less integrated. Skip unless A is too constraining.

### Where to fail safely
Tdarr's `setAllStatus` API + `transcodeUserVerdict` can mark the file
as `Transcode error`, which keeps the source intact and removes the
cache. Combine with `autoAcceptTranscodes: false` for the AV1 library
during the rollout, then flip to true once the VMAF gate has run on a
representative sample.

## 4. Manual queries / state

- API key: `tdarr` k8s secret (key `seeded-api-key`)
- All cruddb collections accessible via POST `/api/v2/cruddb`:
  `{data:{collection:..., mode:..., docID:..., obj:...}}`
  - modes: `getAll`, `getById`, `insert`, `update`, `removeOne`
- Verbose logging on the server: `globalsettings.verboseLogs` in
  `SettingsGlobalJSONDB`
