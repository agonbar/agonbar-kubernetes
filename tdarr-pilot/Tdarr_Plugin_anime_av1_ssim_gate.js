/* AV1 SSIM Gate — DISABLED 2026-04-30. Kept for reference.

   Tdarr loads post-processing plugins inside the server's Node event
   loop and calls them synchronously (the plugin function returns the
   verdict directly). This plugin shells out to `ffmpeg ssim`, which
   for a 24-min 1080p video takes minutes — blocking the event loop
   the whole time. The kubelet liveness probe at `/:8265` then times
   out and kills the pod mid-run. Pod restart loses all the verdict
   state, the file's TranscodeDecisionMaker stays "Queued", and the
   transcode is re-queued forever.

   Verified pattern across 4 attempts on 2026-04-30: every transcode
   produced a clean AV1 output and reached "Plugin 2: running
   Tdarr_Plugin_anime_av1_ssim_gate", then the report truncated and
   the pod restarted (restart count climbed from 0 to 12 in ~80 min).

   Decision: the SSIM check moves into the planned external Tier 2
   acceptance pass alongside libvmaf — neither runs inline. Originals
   are still preserved by `folderToFolderConversionDeleteSource:false`
   so a bad transcode never destroys data; the batch script is the
   single quality gate before the eventual source-deletion sweep.

   This file is kept (rather than deleted) because the SSIM filter
   parsing logic and otherArguments contract are reusable when we
   build the Tier 2 script.
*/

const details = () => ({
  id: 'Tdarr_Plugin_anime_av1_ssim_gate',
  Stage: 'Post-processing',
  Name: 'AV1 SSIM Gate',
  Type: 'Video',
  Operation: 'Other',
  Description: 'After the AV1 transcode is moved to the output folder, '
    + 'compute SSIM(source, transcoded). If below the threshold (default '
    + '0.95) delete the transcoded file. Source is preserved by '
    + 'folderToFolderConversionDeleteSource:false on the library.',
  Version: '1.0',
  Tags: 'post-processing,ffmpeg,quality,ssim,av1,anime',
  Inputs: [
    {
      name: 'minSsim',
      type: 'number',
      defaultValue: 0.95,
      inputUI: { type: 'text' },
      tooltip: 'Minimum acceptable SSIM (0..1). 0.95 ≈ visually transparent. '
        + 'Below this, the transcoded output is deleted and an error logged.',
    },
    {
      name: 'deleteOnFail',
      type: 'boolean',
      defaultValue: true,
      inputUI: { type: 'dropdown', options: ['true', 'false'] },
      tooltip: 'If true (default), delete the transcoded output when SSIM is '
        + 'below the threshold. If false, just log the error (useful for soak '
        + 'testing the threshold before enabling rollback).',
    },
  ],
});

// eslint-disable-next-line @typescript-eslint/no-unused-vars
const plugin = (file, librarySettings, inputs, otherArguments) => {
  const lib = require('../methods/lib')();
  // eslint-disable-next-line no-param-reassign
  inputs = lib.loadDefaultValues(inputs, details);

  const fs = require('fs');
  const path = require('path');
  const { execFileSync } = require('child_process');

  const response = {
    file,
    removeFromDB: false,
    updateDB: false,
    processFile: false,
    error: false,
    infoLog: '',
  };

  const transcodedPath = file.file;
  const originalPath = otherArguments
    && otherArguments.originalLibraryFile
    && otherArguments.originalLibraryFile.file;
  const ffmpegPath = (otherArguments && otherArguments.ffmpegPath) || 'ffmpeg';

  if (!originalPath) {
    response.infoLog += 'No originalLibraryFile path in otherArguments — cannot SSIM-gate. Skipping.\n';
    return response;
  }
  if (!fs.existsSync(originalPath)) {
    response.infoLog += `Original file no longer exists at "${originalPath}" — cannot SSIM-gate. Skipping.\n`;
    return response;
  }
  if (!fs.existsSync(transcodedPath)) {
    response.infoLog += `Transcoded output not found at "${transcodedPath}". Already cleaned up?\n`;
    return response;
  }

  const minSsim = parseFloat(inputs.minSsim);
  const deleteOnFail = String(inputs.deleteOnFail).toLowerCase() === 'true';
  const statsFile = path.join('/tmp', `ssim-${Date.now()}-${Math.random().toString(36).slice(2, 8)}.log`);

  // ssim filter writes per-frame stats AND prints a summary to stderr.
  // We capture stderr and parse the "All:" line. Map only video streams
  // and explicitly fps-match in case container framerates drifted.
  let stderr = '';
  try {
    execFileSync(ffmpegPath, [
      '-hide_banner', '-nostdin',
      '-i', originalPath,
      '-i', transcodedPath,
      '-filter_complex', `[0:v:0][1:v:0]ssim=stats_file=${statsFile}`,
      '-f', 'null', '-',
    ], { stdio: ['ignore', 'ignore', 'pipe'], encoding: 'utf8' });
  } catch (e) {
    stderr = (e.stderr || '').toString();
    if (!stderr) {
      response.error = true;
      response.infoLog += `SSIM ffmpeg failed before producing output: ${e.message}\n`;
      try { fs.unlinkSync(statsFile); } catch (_) { /* nothing to clean */ }
      return response;
    }
  }
  // ffmpeg writes the SSIM summary on stderr even on success; capture
  // both paths via the stdio pipe assignment.
  if (!stderr) {
    // execFileSync doesn't return stderr on success — capture via spawnSync instead.
    const { spawnSync } = require('child_process');
    const r = spawnSync(ffmpegPath, [
      '-hide_banner', '-nostdin',
      '-i', originalPath,
      '-i', transcodedPath,
      '-filter_complex', `[0:v:0][1:v:0]ssim=stats_file=${statsFile}`,
      '-f', 'null', '-',
    ], { encoding: 'utf8' });
    stderr = r.stderr || '';
  }
  try { fs.unlinkSync(statsFile); } catch (_) { /* fine */ }

  // ffmpeg ssim summary line shape:
  //   [Parsed_ssim_0 @ 0x...] SSIM Y:0.987... U:0.992... V:0.991... All:0.989... (19.66)
  const allMatch = stderr.match(/SSIM[^\n]*All:\s*([0-9.]+)/);
  if (!allMatch) {
    response.error = true;
    response.infoLog += `Could not parse SSIM "All:" from ffmpeg stderr. Raw tail:\n${stderr.slice(-512)}\n`;
    return response;
  }

  const score = parseFloat(allMatch[1]);
  response.infoLog += `SSIM(source, transcoded) = ${score.toFixed(4)} (threshold ${minSsim})\n`;

  if (Number.isFinite(score) && score >= minSsim) {
    response.infoLog += 'Pass — transcode kept.\n';
    return response;
  }

  // Below threshold.
  response.error = true;
  if (deleteOnFail) {
    try {
      fs.unlinkSync(transcodedPath);
      response.infoLog += `FAIL — deleted transcoded output "${transcodedPath}". Source preserved at "${originalPath}".\n`;
      // Tdarr's runPostProcPlugins doesn't have a rollback hook; the
      // FileJSONDB will still say "Transcode success" until a rescan.
      // anisub's reconcile phase (or a manual rescan-file API call)
      // will pick up that the output is missing and re-queue.
    } catch (e) {
      response.infoLog += `FAIL — but couldn't delete the output: ${e.message}\n`;
    }
  } else {
    response.infoLog += 'FAIL — deleteOnFail=false, leaving the output in place. Manual review needed.\n';
  }
  return response;
};

module.exports.details = details;
module.exports.plugin = plugin;
