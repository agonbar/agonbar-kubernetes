# VMAF sidecar for the tdarr-node DaemonSet. Ships a libvmaf-capable
# ffmpeg + the vmaf-window CLI into a pod-shared emptyDir. The
# tdarr-node container then shells out to /sidecar/bin/vmaf-window
# from the Tdarr_Plugin_anime_av1_vmaf_gate plugin. The upstream
# tdarr_node image is untouched; image bumps flow through normally.
#
# Static ffmpeg from mwader/static-ffmpeg ships with --enable-libvmaf
# (Debian's package does not). See the validator/Dockerfile for the
# same trick used by the external validator.
FROM mwader/static-ffmpeg:7.1.1 AS ffmpeg

FROM debian:trixie-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ffmpeg /ffmpeg  /usr/local/bin/ffmpeg
COPY --from=ffmpeg /ffprobe /usr/local/bin/ffprobe

# Fail the build if the static ffmpeg doesn't expose libvmaf.
RUN ffmpeg -hide_banner -filters 2>/dev/null | grep -q libvmaf

COPY vmaf-window /opt/vmaf-window
RUN chmod +x /opt/vmaf-window

# On container start, populate the shared /sidecar volume with the
# tools the tdarr-node container will invoke, then idle. The volume is
# emptyDir so we re-cp on every restart (~70 MB, cost negligible).
ENTRYPOINT ["/bin/sh","-c", "\
mkdir -p /sidecar/bin && \
cp -f /usr/local/bin/ffmpeg  /sidecar/bin/ffmpeg && \
cp -f /usr/local/bin/ffprobe /sidecar/bin/ffprobe && \
cp -f /opt/vmaf-window       /sidecar/bin/vmaf-window && \
chmod +x /sidecar/bin/* && \
echo '[vmaf-sidecar] tools published to /sidecar/bin:' && \
ls -la /sidecar/bin && \
exec sleep infinity \
"]
