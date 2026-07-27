ARG BUILD_FROM
FROM ${BUILD_FROM}

# ffmpeg is used for stream-copy remuxing only (MPEG-PS -> fragmented MP4).
# No transcoding, so no hardware acceleration is needed even on a Pi.
RUN apk add --no-cache ffmpeg

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# ── ffmpeg.wasm, vendored at BUILD time ──────────────────────────────────────
# The frontend remuxes clips in the browser, which needs ffmpeg.wasm. It is baked
# into the image rather than pulled from a CDN at runtime, so playing a clip never
# depends on the container having outbound internet — the add-on talks to the DVR
# and nothing else. This is a build-time fetch only, alongside the apk/pip ones
# already above.
#
# WHY THE SINGLE-THREADED CORE (@ffmpeg/core, not @ffmpeg/core-mt) — VERIFIED
# 2026-07-27, DO NOT "UPGRADE" THIS TO core-mt:
# core-mt needs SharedArrayBuffer, which needs the page to be cross-origin
# isolated. Under HA Ingress that is IMPOSSIBLE: isolation is inherited top-down
# and HA's own frontend sends neither COOP nor COEP (checked live on 10.10.10.9),
# so no header this add-on sets can make crossOriginIsolated true inside the
# panel iframe. The ST core needs none of that, and for `-c copy` it is not even
# slower — remuxing a real 3.7 MB DVR clip measured 365 ms with SharedArrayBuffer
# undefined. Stream copying is I/O, not parallel compute; threads buy nothing.
ARG FFMPEG_WASM_VERSION=0.12.15
ARG FFMPEG_CORE_VERSION=0.12.10
ARG FFMPEG_WASM_SHA256=c8a23365fb39b46d3d1d9baa2e74b522d00ce5d57e8b20471ad2665eaad38e3e
ARG FFMPEG_CORE_SHA256=d00089ce82e1bdf637ddbe42e0c3d41a1ba8cf4c9e825e7fa4d0bb970e844bd4
RUN apk add --no-cache --virtual .vendor-deps curl tar \
    && mkdir -p /www/vendor /tmp/vendor \
    && curl -fsSL -o /tmp/vendor/ffmpeg.tgz \
       "https://registry.npmjs.org/@ffmpeg/ffmpeg/-/ffmpeg-${FFMPEG_WASM_VERSION}.tgz" \
    && curl -fsSL -o /tmp/vendor/core.tgz \
       "https://registry.npmjs.org/@ffmpeg/core/-/core-${FFMPEG_CORE_VERSION}.tgz" \
    && echo "${FFMPEG_WASM_SHA256}  /tmp/vendor/ffmpeg.tgz" | sha256sum -c - \
    && echo "${FFMPEG_CORE_SHA256}  /tmp/vendor/core.tgz" | sha256sum -c - \
    && tar xzf /tmp/vendor/ffmpeg.tgz -C /tmp/vendor \
         package/dist/umd/ffmpeg.js package/dist/umd/814.ffmpeg.js \
    && tar xzf /tmp/vendor/core.tgz -C /tmp/vendor \
         package/dist/umd/ffmpeg-core.js package/dist/umd/ffmpeg-core.wasm \
    && cp /tmp/vendor/package/dist/umd/ffmpeg.js \
          /tmp/vendor/package/dist/umd/814.ffmpeg.js \
          /tmp/vendor/package/dist/umd/ffmpeg-core.js \
          /tmp/vendor/package/dist/umd/ffmpeg-core.wasm \
          /www/vendor/ \
    && rm -rf /tmp/vendor \
    && apk del .vendor-deps \
    # 814.ffmpeg.js is the worker the UMD bundle loads by RELATIVE path from
    # wherever ffmpeg.js was served. Both must stay side by side in /www/vendor.
    && test -s /www/vendor/ffmpeg-core.wasm \
    && test -s /www/vendor/814.ffmpeg.js

COPY app/ /app/hikvision_recordings/app/
RUN touch /app/hikvision_recordings/__init__.py
COPY www/ /www/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    ADDON_WWW_DIR=/www \
    ADDON_OPTIONS_PATH=/data/options.json

CMD ["/run.sh"]
