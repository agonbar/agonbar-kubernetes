# DESIGN — in-Tdarr VMAF gate via sidecar (proposed 2026-05-26)

Move the AV1 quality gate from the external `tdarr-validator` Deployments
into Tdarr's own pipeline, without forking the upstream `tdarr_node`
image. The external validator stays as a backfill / safety net.

Status: **design, pre-implementation**. Sign off on §"Open questions"
before any worker-touching change ships.

## Why now

The earlier inline gate (`Tdarr_Plugin_anime_av1_ssim_gate.js`, kept on
disk for reference) was a **`Stage: 'Post-processing'`** plugin. Tdarr
runs those *on the server pod*, inside the server's Node event loop,
synchronously. A multi-minute SSIM/VMAF computation blocks the loop,
the kubelet liveness probe at `:8265` times out, the server pod gets
killed mid-run, the verdict is lost, and the file re-queues forever.
Verified 2026-04-30: restart count 0 → 12 in 80 minutes.

A **`Stage: 'Pre-processing'`** plugin runs on the *worker* pod. The
worker has no comparable tight probe — it routinely blocks for 30–120
minutes inside libsvtav1 — so a 5–10 min VMAF run is well within
tolerance. This is the safe stage to host the gate.

The remaining obstacle was that the bundled `tdarr_node` image's
ffmpeg has no `--enable-libvmaf`. A sidecar container in the same pod
ships a libvmaf-capable ffmpeg into a shared volume; the plugin shells
out to that binary. Upstream `tdarr_node` updates flow through
unmodified — only the DaemonSet manifest grows a second container.

## Architecture

```
DaemonSet: tdarr-node (workload=media, 1 pod per node)
├── container: tdarr-node               (upstream ghcr.io/haveagitgat/tdarr_node:2.70.01)
│     volumeMounts:
│       - /media          (NFS, shared with server + validators)
│       - /sidecar        (emptyDir, populated by sidecar at startup)
│     entrypoint: unchanged
│
└── container: vmaf-sidecar              (our image — static-ffmpeg + vmaf-window CLI)
      volumeMounts:
        - /sidecar         (emptyDir, same)
      command: ["/bin/sh","-c","cp /usr/local/bin/ffmpeg /usr/local/bin/ffprobe /sidecar/bin/ ; cp /opt/vmaf-window /sidecar/bin/ ; exec sleep infinity"]
      resources: small (idle most of the time; ffmpeg runs in tdarr-node's cgroup)
```

`emptyDir` is pod-scoped and visible to every container in the pod, so
the cp at sidecar startup gives the tdarr-node container a stable
binary path at `/sidecar/bin/{ffmpeg,ffprobe,vmaf-window}`. The
sidecar then just `sleep infinity` — its only job is to keep that
volume populated. (We could mount the sidecar's ffmpeg directly with a
read-only bind, but the emptyDir-with-cp pattern is simpler and works
unchanged on any CRI.)

### Why CLI tool, not HTTP

Earlier discussion considered a tiny HTTP server in the sidecar. Going
with a CLI shipped to `/sidecar/bin/vmaf-window` instead:

- One fewer process to keep alive (no port, no socket lifecycle).
- ffmpeg runs in the tdarr-node container's cgroup → CPU/mem
  accounting stays with the existing worker limits (already sized for
  libsvtav1 — 100 GiB RAM, 12 cpu). The sidecar stays tiny.
- The plugin's `execFileSync` shape matches the existing
  `Tdarr_Plugin_anime_av1_ssim_gate.js` exactly — minimal new
  surface area.

`vmaf-window` is a small Python script that wraps the
`validate(...)` function from `tdarr/validator/validator.py` and
prints a single JSON line to stdout. The libvmaf invocation is the
exact same one the external validator uses (input-seek, 3 windows at
10/50/90%, `format=yuv420p10le`, `pool=mean`) — all the gotchas in
`architecture/libvmaf-measurement-gotchas` already encoded.

## Plugin contract

`Tdarr_Plugin_anime_av1_vmaf_gate.js`, registered as
`Stage: 'Pre-processing'`, `Operation: 'Other'`, hooked into the
`anime_health` library's `pluginIDs` *after* the transcode plugin.

Tdarr's `reQueueAfter: true` flag on the transcode plugin causes the
file to be re-presented after the transcode completes. On that second
pass the worker sees:

- `file.file` → the AV1 output (`/media/tv-pilot/.../ep.mkv`)
- `otherArguments.originalLibraryFile.file` → the h264 source
  (`/media/tv/.../ep.mkv`) — still preserved because
  `folderToFolderConversionDeleteSource: false`

The new plugin's responsibilities (synchronous, on the worker):

1. If primary video stream is *not* `av01`, skip (this is the first
   transcode pass, not our turn).
2. Execute `/sidecar/bin/vmaf-window <src> <out>` — emits one JSON
   line with `{verdict, vmaf_worst, vmaf_mean, seconds}`.
3. On `verdict=pass`: `fs.renameSync(out, src)`. Then either (see
   open question §a) update Tdarr's FileJSONDB via the cruddb API to
   reset the row, or leave it for the next library scan.
4. On `verdict=fail`: log the score, return `error: true`, leave
   both files. Manual review or external validator picks it up.

Return shape: `processFile: false` in both cases — there is no
further ffmpeg work to do.

## What the external validator does after this lands

Two clean options, picking one before rollout:

- **Plugin authoritative, external validator deprecated.** Cleanest
  workflow. But we lose the validator's fingerprint cache + audit
  trail, and pre-plugin transcodes (everything the validator has
  already validated and is still working through) need to be drained
  first.
- **Plugin authoritative for new transcodes, external validator
  catches the rest.** Less integrated but simpler migration: the
  validator is idempotent via fingerprints so it's harmless to keep
  running. It naturally winds down as Tdarr's pipeline starts
  handling fresh transcodes inline.

Default to the second; revisit once the inline gate has been clean
for a week.

## Open questions (need answers before coding the plugin)

a. **FileJSONDB cleanup after rename.** After the plugin renames
   `out` → `src`, Tdarr's FileJSONDB still has a row keyed by `out`.
   Options:
   - Plugin POSTs `cruddb mode:removeOne docID:<out>` to drop the
     stale row, then `mode:update docID:<src>` to mark the original
     `Transcode success`. Requires the API key in the plugin —
     accessible to the worker pod via env (we'd add `TDARR_API_KEY`
     to the DaemonSet from the existing `tdarr` secret).
   - Do nothing; let the next library scan reconcile. Simpler, but
     the UI shows phantom rows until then.
   - Have the external validator continue to handle DB cleanup
     (which it does implicitly via "src no longer exists" filter).

b. **Failure visibility.** When VMAF fails, where should that surface?
   `infoLog` + `error: true` shows in the Tdarr JobReport. Probably
   enough. Alternative: also write a `failed-YYYYMMDD-tdarr.jsonl`
   audit on the NFS share parallel to the external validator's logs.

c. **Rollout sequencing.** The plugin can't ship until: (1) the
   sidecar lands in the DaemonSet on all 3 nodes (transparent change
   — tdarr-node container untouched), (2) the binary exists at
   `/sidecar/bin/vmaf-window` (test inside a worker pod with `exec
   ls /sidecar/bin/`), (3) one test run via a single-file Library
   exercise the full path. Only then add the plugin to
   `anime_health.pluginIDs`. The current 3-shard external validator
   keeps running through all of this.

d. **Atomicity concern.** Worst case: plugin computes VMAF=pass,
   crashes right before `fs.renameSync`. Source + output both still
   on disk, FileJSONDB still says `Transcode success` for the output
   row. On worker restart the file is re-presented; plugin re-runs
   VMAF (a few minutes wasted), passes again, completes the rename.
   Acceptable — same idempotency story as the external validator.

## Implementation plan (once §"Open questions" answered)

1. **Sidecar image** (`tdarr/vmaf-sidecar/Dockerfile` + `vmaf-window`)
   — multi-stage build copying static-ffmpeg + the small Python CLI.
   Build target: `ghcr.io/agonbar/tdarr-vmaf-sidecar:latest`.

2. **DaemonSet patch** (`deployments/piracy/tdarr-node.yml`) — add the
   sidecar container, an `emptyDir` volume, the `/sidecar` mount on
   both containers. Image tag for tdarr-node container itself stays
   on 2.70.01.

3. **Plugin** (`tdarr/plugins/Tdarr_Plugin_anime_av1_vmaf_gate.js`).
   Modeled on the SSIM gate (parse logic reuse) but Pre-processing,
   shells out to `/sidecar/bin/vmaf-window`. NOT yet registered in
   any library.

4. **End-to-end test on one file**: pick a single show, override the
   anime_health library config in a copy or run a one-file
   exercise via the UI / cruddb API. Verify the chain transcode →
   reQueueAfter → vmaf_gate → rename completes cleanly.

5. **Library cutover**: append the new plugin id to
   `anime_health.pluginIDs`. The external 3-shard validator stays
   running; new transcodes get gated inline, old ones keep flowing
   through the validator.

6. **Vault note update**: `projects/tdarr.md` provenance entry +
   `architecture/libvmaf-measurement-gotchas` cross-link.
