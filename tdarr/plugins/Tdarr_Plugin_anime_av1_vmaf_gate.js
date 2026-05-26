/* Anime AV1 VMAF Gate — runs after Tdarr_Plugin_anime_av1_pilot has
   transcoded the source to AV1. Computes libvmaf over 3 windows via
   the sidecar binary at /sidecar/bin/vmaf-window. On pass: atomically
   replaces the original h264 source with the AV1 output and cleans up
   Tdarr's FileJSONDB so the UI reflects the swap. On fail: leaves both
   files in place, returns error so the file shows up in Tdarr's
   errored filter for review.

   This is a `Pre-processing` stage plugin specifically so it runs on
   the WORKER pod, not the server. The earlier SSIM gate
   (`Tdarr_Plugin_anime_av1_ssim_gate.js`, kept on disk for reference)
   was Post-processing — that runs in the server's Node event loop and
   gets killed by the kubelet liveness probe on long ffmpeg runs.
   Workers routinely block for 30-120 min on libsvtav1 with no probe
   issues, so synchronous VMAF for ~5-10 min per file is safe here.

   Sidecar comes from deployments/piracy/tdarr-node.yml (vmaf-sidecar
   container in the same DaemonSet pod). The vmaf-window CLI prints
   one JSON line — see tdarr/vmaf-sidecar/vmaf-window.

   See tdarr/docs/DESIGN-vmaf-sidecar.md for the full design.

   How this plugs in:
     - Library: anime_health, pluginIDs ordered as
         [Tdarr_Plugin_anime_av1_pilot, Tdarr_Plugin_anime_av1_vmaf_gate]
     - Pilot plugin returns reQueueAfter:true, so after transcode the
       file (now AV1 in /media/tv-pilot/...) is re-presented. This
       plugin filters on AV1 primary video and runs the gate. */

const details = () => ({
  id: 'Tdarr_Plugin_anime_av1_vmaf_gate',
  Stage: 'Pre-processing',
  Name: 'Anime AV1 VMAF Gate',
  Type: 'Video',
  Operation: 'Other',
  Description: 'After Tdarr_Plugin_anime_av1_pilot produces an AV1 output, '
    + 'this plugin invokes the vmaf-sidecar to compute VMAF over 3 windows '
    + '(10%/50%/90% of duration, worst-window gating). On pass (VMAF >= '
    + 'minVmaf), atomically renames the AV1 file over the original h264 '
    + 'source and updates the FileJSONDB to reflect the swap. On fail, '
    + 'leaves both files in place and marks the file as errored.',
  Version: '1.0',
  Tags: 'pre-processing,quality,vmaf,av1,anime',
  Inputs: [
    {
      name: 'minVmaf',
      type: 'number',
      defaultValue: 95.0,
      inputUI: { type: 'text' },
      tooltip: 'Worst-window VMAF threshold. Below this, the AV1 file is '
        + 'kept but the file is marked errored. 95 is "visually transparent" '
        + 'per the bench (median 97.4).',
    },
    {
      name: 'sidecarBin',
      type: 'string',
      defaultValue: '/sidecar/bin/vmaf-window',
      inputUI: { type: 'text' },
      tooltip: 'Path to the vmaf-window CLI inside the worker container, '
        + 'populated by the vmaf-sidecar container in the same pod.',
    },
  ],
});

// eslint-disable-next-line @typescript-eslint/no-unused-vars
const plugin = (file, librarySettings, inputs, otherArguments) => {
  const lib = require('../methods/lib')();
  // eslint-disable-next-line no-param-reassign
  inputs = lib.loadDefaultValues(inputs, details);

  const fs = require('fs');
  const { spawnSync } = require('child_process');

  const response = {
    file,
    removeFromDB: false,
    updateDB: false,
    processFile: false,
    error: false,
    reQueueAfter: false,
    infoLog: '',
  };

  const out = file.file;                    // AV1 output (e.g. /media/tv-pilot/.../ep.mkv)
  const src = otherArguments
    && otherArguments.originalLibraryFile
    && otherArguments.originalLibraryFile.file;  // original h264 source

  if (!src) {
    response.infoLog += 'No originalLibraryFile in otherArguments — '
      + 'this plugin only runs on re-queued AV1 outputs. Skipping.\n';
    return response;
  }

  // Filter: skip the pre-transcode first pass. Only re-queued AV1
  // outputs should hit the VMAF check.
  const streams = (file.ffProbeData && file.ffProbeData.streams) || [];
  let primaryCodec = null;
  for (let i = 0; i < streams.length; i++) {
    const s = streams[i];
    if (s.codec_type === 'video' && s.codec_name !== 'mjpeg' && s.codec_name !== 'png') {
      primaryCodec = s.codec_name;
      break;
    }
  }
  if (primaryCodec !== 'av1') {
    response.infoLog += `Primary video codec is ${primaryCodec} (expected av1). `
      + 'This pass is the pre-transcode visit; the gate runs on the post-transcode re-queue. Skipping.\n';
    return response;
  }

  if (!fs.existsSync(src)) {
    response.infoLog += `Original source "${src}" no longer exists — already swapped. Skipping.\n`;
    return response;
  }
  if (!fs.existsSync(out)) {
    response.infoLog += `AV1 output "${out}" missing. Cannot gate. Erroring.\n`;
    response.error = true;
    return response;
  }

  const minVmaf = parseFloat(inputs.minVmaf);
  const sidecarBin = String(inputs.sidecarBin);

  if (!fs.existsSync(sidecarBin)) {
    response.error = true;
    response.infoLog += `vmaf-window CLI not found at "${sidecarBin}". `
      + 'Is the vmaf-sidecar container running in this pod?\n';
    return response;
  }

  // Run the gate. Synchronous — the worker pod has no tight liveness
  // probe and routinely blocks for tens of minutes on libsvtav1.
  const r = spawnSync(sidecarBin, [src, out], {
    encoding: 'utf8',
    maxBuffer: 32 * 1024 * 1024,
    timeout: 30 * 60 * 1000,   // 30 min hard cap per file
  });
  if (r.status !== 0 || !r.stdout) {
    response.error = true;
    response.infoLog += `vmaf-window failed (exit ${r.status}). `
      + `stderr tail: ${(r.stderr || '').slice(-512)}\n`;
    return response;
  }

  let verdict;
  try {
    verdict = JSON.parse(r.stdout.trim().split('\n').pop());
  } catch (e) {
    response.error = true;
    response.infoLog += `Could not parse vmaf-window JSON: ${e.message}. `
      + `Raw: ${r.stdout.slice(-512)}\n`;
    return response;
  }

  if (verdict.verdict !== 'pass') {
    response.error = true;
    response.infoLog += `VMAF gate FAIL: verdict=${verdict.verdict} `
      + `worst=${verdict.vmaf_worst} mean=${verdict.vmaf_mean} `
      + `windows=${JSON.stringify(verdict.windows)} seconds=${verdict.seconds}. `
      + 'Both files left on disk for manual review.\n';
    return response;
  }

  if (verdict.vmaf_worst < minVmaf) {
    response.error = true;
    response.infoLog += `VMAF gate FAIL: worst=${verdict.vmaf_worst} < minVmaf=${minVmaf}. `
      + 'Both files left on disk.\n';
    return response;
  }

  // Pass — atomically replace the original h264 source with the AV1 output.
  // os.replace equivalent: fs.renameSync is atomic on the same filesystem.
  // Both paths sit on the same /media NFS mount.
  try {
    fs.renameSync(out, src);
  } catch (e) {
    response.error = true;
    response.infoLog += `VMAF passed (worst=${verdict.vmaf_worst}) but rename failed: ${e.message}. `
      + 'Both files left in place.\n';
    return response;
  }

  response.infoLog += `VMAF gate PASS: worst=${verdict.vmaf_worst} mean=${verdict.vmaf_mean} `
    + `seconds=${verdict.seconds}. Renamed AV1 over h264 source: "${src}".\n`;

  // Disk is now the source of truth. Tell Tdarr to drop the stale
  // /media/tv-pilot/... row (the file moved out from under that path).
  // The original /media/tv/... row stays as-is — Tdarr's next library
  // scan re-probes it, sees AV1 codec, and the pilot plugin's
  // "only convert h264" guard keeps it from being re-encoded.
  response.removeFromDB = true;
  return response;
};

module.exports.details = details;
module.exports.plugin = plugin;
