/* Anime AV1 Pilot — h264 -> libsvtav1 (preset 4 / CRF 26) with per-channel
   Opus audio, copied subtitles + attachments. Target container: .mkv.
   Skips anything that is not h264. Safe to install alongside the existing
   "tv" library — only the dedicated pilot library should reference it.

   Recipe validated by tdarr/docs/BENCH-REPORT.md. */

const details = () => ({
  id: 'Tdarr_Plugin_anime_av1_pilot',
  Stage: 'Pre-processing',
  Name: 'Anime AV1 Pilot (svtav1 + Opus)',
  Type: 'Video',
  Operation: 'Transcode',
  Description: 'Transcodes h264 to AV1 (libsvtav1 preset 4, CRF 26, 10-bit) '
    + 'with per-channel Opus audio (96k stereo, 256k 5.1, 384k 7.1). '
    + 'Subtitles and attachments are copied. Non-h264 sources are skipped.',
  Version: '1.0',
  Tags: 'pre-processing,ffmpeg,video only,h264,av1,svtav1,opus,anime',
  Inputs: [
    {
      name: 'crf',
      type: 'number',
      defaultValue: 26,
      inputUI: { type: 'text' },
      tooltip: 'SVT-AV1 CRF (constant quality). Lower = better quality, larger file. Default 26.',
    },
    {
      name: 'svt_preset',
      type: 'number',
      defaultValue: 4,
      inputUI: { type: 'text' },
      tooltip: 'SVT-AV1 preset 0-13. Lower = slower = better compression. Default 4.',
    },
  ],
});

// eslint-disable-next-line @typescript-eslint/no-unused-vars
const plugin = (file, librarySettings, inputs, otherArguments) => {
  const lib = require('../methods/lib')();
  // eslint-disable-next-line no-param-reassign
  inputs = lib.loadDefaultValues(inputs, details);

  const response = {
    processFile: false,
    preset: '',
    container: '.mkv',
    handBrakeMode: false,
    FFmpegMode: true,
    reQueueAfter: true,
    infoLog: '',
  };

  if (file.fileMedium !== 'video') {
    response.infoLog += 'Not a video file. Skipping.\n';
    return response;
  }

  const streams = (file.ffProbeData && file.ffProbeData.streams) || [];

  // Find the primary video stream (first non-cover, non-png).
  let primaryVideoCodec = null;
  for (let i = 0; i < streams.length; i++) {
    const s = streams[i];
    if (s.codec_type === 'video' && s.codec_name !== 'mjpeg' && s.codec_name !== 'png') {
      primaryVideoCodec = s.codec_name;
      break;
    }
  }
  if (primaryVideoCodec === null) {
    response.infoLog += 'No primary video stream found. Skipping.\n';
    return response;
  }
  if (primaryVideoCodec !== 'h264') {
    response.infoLog += `Primary video codec is ${primaryVideoCodec}; only h264 sources are converted by this plugin. Skipping.\n`;
    return response;
  }

  // Build per-output-audio-stream Opus arguments.
  // Bitrate selected by channel count: <=2ch=96k, 3-6ch=256k, 7+ch=384k.
  let audioArgs = '';
  let outAudioIdx = 0;
  for (let i = 0; i < streams.length; i++) {
    const s = streams[i];
    if (s.codec_type !== 'audio') continue;
    const ch = parseInt(s.channels, 10) || 2;
    let br;
    if (ch >= 7) br = '384k';
    else if (ch >= 5) br = '256k';
    else br = '96k';
    audioArgs += `-c:a:${outAudioIdx} libopus -b:a:${outAudioIdx} ${br} `;
    outAudioIdx += 1;
  }
  if (outAudioIdx === 0) {
    response.infoLog += 'Source has no audio streams; this pilot expects at least one. Skipping.\n';
    return response;
  }

  const crf = parseInt(inputs.crf, 10);
  const svtPreset = parseInt(inputs.svt_preset, 10);

  // ffmpeg args: leading "," signals no input-side overrides.
  // Map primary video, all audio, optional subs/attachments. Drop cover-art (mjpeg/png) by selecting v:0 only.
  response.preset = ','
    + '-map 0:v:0 -map 0:a -map 0:s? -map 0:t? '
    + `-c:v libsvtav1 -preset ${svtPreset} -crf ${crf} -pix_fmt yuv420p10le `
    + audioArgs
    + '-c:s copy '
    + '-map_metadata 0 -map_chapters 0 '
    + '-max_muxing_queue_size 9999';
  response.processFile = true;
  response.infoLog += `Transcoding h264 -> AV1 (svtav1 preset=${svtPreset} crf=${crf}); `
    + `${outAudioIdx} audio stream(s) -> Opus (per-channel bitrate).\n`;
  return response;
};

module.exports.details = details;
module.exports.plugin = plugin;
