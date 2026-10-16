#!/usr/bin/env node
// Decode or edit a Steam Remote Play client config (CStreamingClientConfig
// protobuf, base64 as SteamClient.RemotePlay.SetStreamingClientConfig takes it).
//
//   scripts/steam-remoteplay-config.mjs <base64>                  # print fields
//   scripts/steam-remoteplay-config.mjs <base64> 35=1 39=1 2=2560 # print new base64
//
// Varint fields take a number; nested messages take n=hex:<bytes>. Field
// numbers, from the client's steamui bundle (2026-10): 2/3
// desired_resolution_x/y, 4/5 framerate num/den, 13 enable_video_hevc,
// 21 display_limit, 26 enable_video_av1, 35 enable_video_pyrowave,
// 39 pyrowave_yuv444, 40 pyrowave_bitrate_kbps.
//
// display_limit is a snapshot of the client's display (field 2: {1 width,
// 2 height, 4/5 refresh num/den}). Restoring an old config restores an old
// snapshot: one taken in handheld mode caps every stream at 40 Hz -> 30 fps.
const [b64, ...sets] = process.argv.slice(2);
const buf = Buffer.from(b64, "base64");

function readVarint(b, i) {
  let v = 0n, s = 0n;
  for (;;) { const c = b[i++]; v |= BigInt(c & 0x7f) << s; s += 7n; if (c < 0x80) return [v, i]; }
}
function writeVarint(v) {
  const out = []; v = BigInt.asUintN(64, BigInt(v));
  do { let c = Number(v & 0x7fn); v >>= 7n; if (v) c |= 0x80; out.push(c); } while (v);
  return Buffer.from(out);
}

// Fields in wire order: { n, wt, value (BigInt) | raw (Buffer) }
const fields = [];
for (let i = 0; i < buf.length;) {
  let key; [key, i] = readVarint(buf, i);
  const n = Number(key >> 3n), wt = Number(key & 7n);
  if (wt === 0) { let v; [v, i] = readVarint(buf, i); fields.push({ n, wt, value: v }); }
  else if (wt === 2) { let len; [len, i] = readVarint(buf, i); fields.push({ n, wt, raw: buf.subarray(i, i + Number(len)) }); i += Number(len); }
  else if (wt === 5) { fields.push({ n, wt, raw: buf.subarray(i, i + 4) }); i += 4; }
  else if (wt === 1) { fields.push({ n, wt, raw: buf.subarray(i, i + 8) }); i += 8; }
  else throw new Error(`wire type ${wt} at byte ${i}`);
}

if (!sets.length) {
  for (const f of fields) console.log(f.n, f.wt === 0 ? f.value.toString() : f.raw.toString("hex"));
  process.exit(0);
}

for (const s of sets) {
  const [k, v] = s.split("=");
  const n = Number(k);
  // n=hex:<bytes> replaces a length-delimited field (nested message) verbatim.
  const nf = v.startsWith("hex:")
    ? { n, wt: 2, raw: Buffer.from(v.slice(4), "hex") }
    : { n, wt: 0, value: BigInt(v) };
  const i = fields.findIndex((x) => x.n === n);
  if (i >= 0) fields[i] = nf; else fields.push(nf);
}
fields.sort((a, b) => a.n - b.n);
const out = Buffer.concat(fields.map((f) => f.wt === 0
  ? Buffer.concat([writeVarint((f.n << 3) | 0), writeVarint(f.value)])
  : f.wt === 2
    ? Buffer.concat([writeVarint((f.n << 3) | 2), writeVarint(f.raw.length), f.raw])
    : Buffer.concat([writeVarint((f.n << 3) | f.wt), f.raw])));
console.log(out.toString("base64"));
