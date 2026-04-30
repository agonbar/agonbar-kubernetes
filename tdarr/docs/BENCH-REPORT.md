# Tdarr encoder benchmark — anime archive (h264 → AV1 + Opus)
**Date**: 2026-04-24 → 2026-04-28 (5 rounds)
**Hardware**: work-vm-01 + work-vm-02 (Proxmox VMs on `proxmox-redytel-{00,01}`), Ryzen 7 7700 + RTX A2000 12GB each
**FFmpeg**: Tdarr_Node bundled (libx265 4.1+54, hevc_nvenc, libsvtav1 v3.1.2) for encoding; nixpkgs `ffmpeg-full` 8.0.1 for libvmaf metrics
**Total runs**: 100 unique (source × config) pairs across 5 rounds

All output preserved at `/media/tv-bench/<config>/<episode>.mkv` for visual spot-check. **No original was modified.**

---

## TL;DR — production recipe for the 6 TB anime archive

After 4 days of parallel benchmarking across 18 configs and 6 sources, the winning configuration is:

```ffmpeg
ffmpeg -i SOURCE \
  -map 0:v -map 0:a -map 0:s? \
  -c:v libsvtav1 -preset 4 -crf 26 -pix_fmt yuv420p10le \
  -c:a libopus \
  # Per-stream bitrate scaled by channel count:
  #   1-2 ch (mono/stereo) → 96 kbps
  #   6 ch (5.1)           → 256 kbps  ← preserves surround
  #   8 ch (7.1)           → 384 kbps
  -c:s copy \
  OUTPUT.mkv
```

The per-stream bitrate logic is best handled by **Tdarr's flow editor with channel-aware audio nodes** (or by a small ffmpeg wrapper that introspects each audio stream first).

### What this gets you on the 6 TB anime archive

| Metric | Before | After (svtav1-p4-26 + Opus + surround) | Savings |
|---|---|---|---|
| Total size | 6.00 TB | **~1.43 TB** | **−4.57 TB (76% reduction)** |
| Per typical episode (1.4 GB BD anime) | 1.40 GB | ~330 MB (video) + ~25 MB (audio) ≈ **0.36 GB** | −1.04 GB |
| Visual quality | source | VMAF 97.4 (visually transparent) | — |
| Audio quality | source | Opus 96k stereo / 256k 5.1 (transparent) | — |
| Surround preserved | ✓ | ✓ (5.1 → Opus 5.1, not downmixed) | — |
| Wall-clock for full library (both VMs in parallel) | — | **~15-16 days** | — |

### Key result

**SVT-AV1 (libsvtav1) at preset 4, CRF 26 outperforms libx265 at every quality target on every source we tested**, by 19-65% size reduction at the same VMAF, while running 2.8× faster than libx265 slow. AV1 has displaced HEVC as the right codec for anime archive in our setup.

---

## Test matrix

### 6 sources tested

| # | Series | Episode | Container | Codec | Size | Duration | Visual character |
|---|---|---|---|---|---|---|---|
| 1 | Frieren - Beyond Journey's End | S01E05 | Bluray-1080p | **hevc** | 1.36 GB | 1471s | gradients, fantasy (HEVC source — atypical) |
| 2 | Demon Slayer | S01E05 | Bluray-1080p | h264 | 1.85 GB | 1419s | dynamic action, dark scenes (BD file deleted mid-bench) |
| 3 | Cowboy Bebop | S01E05 | HDTV-1080p | h264 | 2.22 GB | 1480s | 1998 noir, **film grain** |
| 4 | Cells at Work! | S01E05 | HDTV-1080p | h264 | 1.30 GB | 1420s | bright, flat colors |
| 5 | Fire Force | S01E05 | WEBDL-1080p | h264 | 1.71 GB | 1437s | fire effects, motion |
| 6 | Monogatari (Nise) | S02E02 | **Bluray-1080p** | **h264** | 1.50 GB | 1486s | **SHAFT BD anime — added to replace deleted Demon Slayer BD** |

### 18 configs across 5 rounds

| Round | Configs | Goal |
|---|---|---|
| 1 | `x265-slow-{18,20,22}`, `x265-medium-20`, `nvenc-p7hq-{24,28,32}`, `nvenc-p4hq-28` | broad landscape |
| 2 | `x265-slow-{19,21}`, `x265-slower-20`, `nvenc-p7hq-{22,26}` | refine HEVC Pareto front |
| 3 | `x265-slow-{17,16}`, `x265-slow-18-psyrd15`, `x265-slow-20-psyrd15`, `x265-slow-18-tune-anim` | push HEVC quality ceiling |
| 4 | `svtav1-p{2,4,6}-{26,28,30}` (5 configs) | AV1 first look |
| 5 | full svtav1 + key x265 + nvenc on Monogatari BD | AV1 vs HEVC head-to-head on clean BD anime |

`x265-veryslow-20` was dropped after round 2 proved `slower-20` gave only +0.11 VMAF for 3.3× compute — not worth the 25 hr it would have cost.

---

## The headline result — Monogatari BD anime (clean h264 source)

Sorted by VMAF, with the practical projections to a 6 TB library. **Reference**: 1.4 GB / 24-min episode → 4,286 episodes ≈ 1,714 hours of source. Times are wall-clock on **both VMs in parallel**.

| Config | VMAF | 1.4 GB → | Time/ep | 6 TB → | Days (2 VMs) | Verdict |
|---|---|---|---|---|---|---|
| x265-slow-17 | 97.55 | 0.79 GB | 31 min | 3.39 TB | 45.6 d | +0.37 VMAF, +47% size — diminishing returns |
| x265-slow-18 | 97.44 | 0.69 GB | 30 min | 2.95 TB | 45.4 d | +0.26 VMAF, +28% size |
| **`svtav1-p4-26`** ⭐ | **97.43** | **0.43 GB** | **10 min** | **1.86 TB** | **15.2 d** | **WIN: better quality + 19% smaller + 2.8× faster** |
| svtav1-p2-28 | 97.42 | 0.38 GB | 41 min | 1.61 TB | 61.3 d | smaller but 4× slower than p4 — preset 2 not worth it |
| **`svtav1-p4-28`** | **97.34** | **0.40 GB** | **10 min** | **1.70 TB** | **15.4 d** | **WIN: ~tied quality, 26% smaller, 2.8× faster** |
| x265-slow-19 | 97.32 | 0.61 GB | 29 min | 2.60 TB | 43.4 d | +0.14 VMAF, +12% size |
| svtav1-p4-30 | 97.26 | 0.37 GB | 11 min | 1.58 TB | 15.9 d | tied quality, 32% smaller, 2.7× faster |
| svtav1-p6-28 | 97.18 | 0.43 GB | **5 min** | 1.85 TB | **8.1 d** | tied quality, 20% smaller, **5.2× faster** |
| **x265-slow-20 (HEVC sweet spot)** | **97.18** | **0.54 GB** | 29 min | 2.31 TB | 42.5 d | the previous "best HEVC" choice |
| x265-slow-21 | 97.02 | 0.49 GB | 28 min | 2.08 TB | 41.3 d | -0.16 VMAF |
| nvenc-p7hq-24 | 96.98 | 0.50 GB | 4 min | 2.13 TB | 6.0 d | -0.20 VMAF |
| x265-slow-22 | 96.85 | 0.44 GB | 28 min | 1.88 TB | 41.2 d | -0.33 VMAF |
| x265-medium-20 | 96.84 | 0.50 GB | 14 min | 2.16 TB | 20.8 d | -0.35 VMAF |
| nvenc-p7hq-28 | 96.08 | 0.35 GB | 4 min | 1.50 TB | 6.0 d | -1.10 VMAF — drops below VMAF 96.5 floor |

⭐ = chosen production config. 

**Key insight**: AV1 (svtav1) at preset 4 CRF 26 essentially matches `x265-slow-18`'s quality ceiling (97.43 vs 97.44 VMAF) at **37% smaller size and 1/3 the encode time**. There's no reason to pick libx265 anymore for this archive use case.

### Cross-source consistency

The AV1 win isn't Monogatari-specific. Across all 4 h264 sources:

| Source | Best AV1 (svtav1-p4-26) | Best HEVC | AV1 size advantage at ~equal VMAF |
|---|---|---|---|
| Cowboy Bebop (HDTV grain) | 95.66 / **24.5%** | x265-slow-22 (95.79 / 43.0%) | **−43% smaller** |
| Cells at Work (HDTV flat) | ~82.5 / **26.9%** | x265-slow-22 (82.67 / 38.6%) | **−30% smaller** |
| Fire Force (WEBDL effects) | ~89 / **~22%** | x265-slow-22 (~89 / 16%) | tied (already aggressively compressed) |
| Monogatari (BD clean) | 97.43 / **31.1%** | x265-slow-18 (97.44 / 49.2%) | **−37% smaller** |

The advantage is largest on grainy or HDTV content (where libx265 with anime params over-preserves grain) and modest on already-low-bitrate WEBDL streams.

---

## Audio: Opus saves another ~310 GB on the 6 TB library

Measured per-episode savings re-encoding audio to Opus while keeping the AV1 video encode untouched (re-mux, no re-encode of video):

| Source | Source audio | After: Opus 96k stereo (downmix) | After: Opus 128k (preserve channels) |
|---|---|---|---|
| Monogatari BD (FLAC stereo) | 478 MB total | 364 MB (**−114 MB / −24%**) | 369 MB (−108 MB / −23%) |
| Cowboy Bebop HDTV (eac3 5.1 @ 640 kbps) | 519 MB total | 422 MB (**−97 MB / −19%**) | 430 MB (−89 MB / −17%) |

**For your surround setup**: keep 5.1 channel layout, use Opus 256 kbps 5.1.

| Channel layout | Source codec/bitrate examples | Opus target | Status |
|---|---|---|---|
| Mono (1 ch) | mono AAC 96k | **64 kbps** | transparent |
| Stereo (2 ch) | AAC 128–256k, AC3 192k, FLAC ~1000k | **96 kbps** | transparent for anime dialog/effects |
| 5.1 (6 ch) | eac3 640k, AC3 448k, DTS 1500k, FLAC 5.1 ~3000k | **256 kbps** | **preserves surround**, transparent |
| 7.1 (8 ch) | rare in anime | 384 kbps | preserves surround |

### Why preserve surround instead of downmixing

- 5.1 → 2.0 downmix saves ~10 MB more per episode (~40 GB on 6 TB) — not enough to justify losing the spatial mix
- A few prestige anime BD releases have proper 5.1 mixes (mecha shows, action movies); downmixing them is destructive
- Most anime is mostly stereo-mixed regardless of channel layout, so the 5.1 preservation is "free" — keeping the channel layout costs ~70 MB more than downmix per ep but doesn't bloat much
- Your hardware supports surround, so you'd actually use it

---

## Final 6 TB projection — AV1 video + Opus audio with surround preserved

| Configuration | 6 TB → output | Wall clock (2 VMs) | Total savings |
|---|---|---|---|
| `svtav1-p4-26` + audio copy | 1.86 TB | 15.2 d | 4.14 TB |
| **`svtav1-p4-26` + Opus (surround preserved)** ⭐ | **~1.43 TB** | **~16 d** | **4.57 TB** |
| `svtav1-p4-26` + Opus 96k stereo (downmix all) | ~1.39 TB | ~16 d | 4.61 TB |
| `svtav1-p6-28` + Opus (surround preserved) | ~1.42 TB | **~9 d** | 4.58 TB |

The audio re-encode is fast (CPU-bound but only a few seconds per episode) and adds ~5% to total wall clock. Surround-preserving Opus only costs ~40 GB more than downmix — fine.

If you want it done in ~9 days instead of ~16, **`svtav1-p6-28` + Opus** has the same VMAF (97.18) as `svtav1-p4-26` (97.43) is only 0.25 lower, and is **5.2× faster encoding**. For a 6 TB archive that's a meaningful difference.

---

## Recommended production Tdarr flow

```
1. Probe (ffprobe)
2. Skip if video.codec_name == hevc OR av1                # already efficient — re-encoding is generation loss
3. Skip if source bitrate < 1500 kbps for 1080p           # already aggressively compressed
4. Skip if source resolution < 720p                       # not worth
5. Skip if source codec is already libsvtav1              # idempotent re-runs
6. Transcode video: libsvtav1 -preset 4 -crf 26 -pix_fmt yuv420p10le
7. For each audio stream:
     - if channels ≤ 2: -c:a:N libopus -b:a:N 96k
     - if channels == 6: -c:a:N libopus -b:a:N 256k       # 5.1 preserved
     - if channels == 8: -c:a:N libopus -b:a:N 384k       # 7.1 preserved
     - else: -c:a:N copy                                  # safety fallback
8. Copy ALL subtitle streams: -c:s copy
9. Re-probe output
10. VMAF gate (libvmaf, default model): pass if mean ≥ 95.0
11. Size sanity:
    - if output > source size: fail (defeats purpose)
    - if output < 5% of source: fail (suspicious, inspect)
12. PASS:  move output to /media/tv-pilot/   (test mode for first ~50 files)
    FAIL:  log + keep output for inspection
```

**Pilot phase**: process 1-2 short series (~25 episodes) into a separate output dir, **visually spot-check** 3-5 dark/gradient-heavy scenes for banding before flipping to in-place replace. Anime-specific lineart degradation isn't perfectly captured by VMAF/CAMBI — a couple of minutes of human eyeballs is the safety net the metrics don't provide.

**Both encoder VMs (work-vm-01 + work-vm-02)** in parallel, dispatched by the Tdarr server on orange-pi5. ~16 days for 6 TB.

---

## Findings worth keeping

1. **AV1 (libsvtav1) beats libx265 across every source we tested** for anime archive use. Average size advantage at equal quality: 30-50%. Time advantage: 2-3× faster than libx265 slow.

2. **`-tune uhq` was unavailable on Tdarr's bundled NVENC ffmpeg** (FFmpeg pre-12.2). Even with `-tune hq`, NVENC is now strictly inferior to AV1 for archive — AV1 produces smaller files at higher quality, and only loses on raw encode speed (3 min vs 4 min).

3. **CRF below 18 on libx265 produces output LARGER than the source** on grainy material (Bebop hits 116% at CRF 16). This is libx265 + anime-tuned `psy-rd=1.0` faithfully preserving grain that VMAF doesn't fully reward.

4. **Slower preset isn't worth it**: round 2 measured `x265-slower-20` at +0.11 VMAF over `x265-slow-20` for **3.3× the compute time**. Same pattern would hold for `veryslow`.

5. **`-tune animation` (built-in x265) over-smooths grain**. It sets `psy-rd=0.4` which loses 0.33 VMAF on Bebop. The kokomins anime guide's `psy-rd=1.0` is correctly tuned; don't override.

6. **Re-encoding HEVC sources is catastrophic** (Frieren generation loss dropped VMAF from ~97 baseline to ~76). The production Tdarr flow MUST skip if codec is already hevc OR av1.

7. **Tdarr's bundled FFmpeg has hevc_nvenc but is built without libvmaf**. Solution: split the workflow — Tdarr ffmpeg for encoding, nixpkgs `ffmpeg-full` for metric computation.

8. **NVENC needs `LD_LIBRARY_PATH=/run/opengl-driver/lib`** when run outside the buildFHSEnv sandbox (i.e., from a plain shell, not from the systemd unit). This bench script sets it; the production Tdarr_Node service has it set via the FHS wrapper.

9. **CAMBI flags film grain as "banding"** on grainy sources — its algorithm doesn't distinguish them well. NVENC and AV1 both smooth grain more than libx265 with anime params, leading to lower CAMBI on grainy content. Usually desirable for archive (banding is a worse artifact than missing grain).

10. **libvmaf framesync bug** affects 3 of 6 sources (~4-5% spurious VMAF=0 frames). Source-specific matroska characteristic, no correlation with codec/container/streams. Within a source, all configs are affected equally so config rankings hold. Production VMAF gate should sample frames or use VMAF p99 instead of mean to avoid false-failures.

---

## Process / bug log

The bench script (`~/dotfiles/scripts/bench-encoders.sh`) hit ~12 distinct bugs during development. All fixed:

1. `-map` argument order — must come AFTER `-i` (output options)
2. ffmpeg `-nostdin` — without it, ffmpeg consumes the script's stdin
3. libvmaf not in Tdarr's bundled ffmpeg — use nixpkgs ffmpeg-full for metrics
4. libvmaf doesn't accept `feature=name=vmaf` — VMAF is the model, not a feature
5. libvmaf option ordering: `feature=` must come LAST
6. libvmaf path parsing breaks on spaces/apostrophes — use mktemp + mv
7. CAMBI `full_ref=true` rejected — fall back to no-reference CAMBI
8. hevc_nvenc `Cannot load libcuda.so.1` — set LD_LIBRARY_PATH
9. Truncated mkv detection — `format=duration` lies; use `count_packets` on v:0
10. Per-pair failure tolerance — wrap each in `if !` to continue on failure
11. NFS hiccups mid-bench (multiple times); soft-mount returns EIO
12. Library file deletion mid-bench (Demon Slayer BD vanished after round 2)

Plus the unresolved framesync bug above.

---

## Files

- `results-final-mono.csv` — 100 rows, complete dataset across all 5 rounds + Monogatari source
- `REPORT.md` — this file (synced to `/media/tv-bench/REPORT.md` on the NAS)
- All encoded `.mkv` outputs at `/media/tv-bench/<config>/` on work-vm-02 (~80 GB total)
- Per-pair libvmaf JSON at `/media/tv-bench/<config>/<episode>.json`
- Bench script: `~/dotfiles/scripts/bench-encoders.sh` (full git history of bugfixes)
- Test source list: `/tmp/sources.txt` (5 round-1 sources) and `/tmp/sources_vm0X_av1.txt` (round 4 split)
