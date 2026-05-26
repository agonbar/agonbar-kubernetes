# Debian's ffmpeg package isn't built with --enable-libvmaf, so we pull a
# static ffmpeg binary that includes libvmaf from mwader/static-ffmpeg
# (the standard ffmpeg-with-vmaf image in the ecosystem). Python 3 + the
# standard library is the only additional runtime requirement.
FROM mwader/static-ffmpeg:7.1.1 AS ffmpeg

FROM debian:trixie-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        python3 \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ffmpeg /ffmpeg  /usr/local/bin/ffmpeg
COPY --from=ffmpeg /ffprobe /usr/local/bin/ffprobe

# Fail the build if the static ffmpeg doesn't expose libvmaf.
RUN ffmpeg -hide_banner -filters 2>/dev/null | grep -q libvmaf

COPY validator.py /app/validator.py
WORKDIR /app
ENTRYPOINT ["python3", "/app/validator.py"]
