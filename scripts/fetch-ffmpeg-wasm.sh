#!/usr/bin/env bash
# Vendor ffmpeg.wasm into hikvision_recordings/www/vendor/ for LOCAL development.
#
# The add-on image does this itself at build time (see the Dockerfile) — this
# script exists only so a local checkout can exercise the client-remux path, and
# it pins the same versions and the same checksums so the two cannot drift.
#
# The files are NOT committed: ffmpeg-core.wasm alone is ~32 MB, which does not
# belong in a git history that ships to every add-on update.
#
# Without running this, the frontend still works — FFmpegWASM is simply undefined
# and app.js falls back to the server-side /api/stream path.
set -euo pipefail

FFMPEG_WASM_VERSION=0.12.15
FFMPEG_CORE_VERSION=0.12.10
FFMPEG_WASM_SHA256=c8a23365fb39b46d3d1d9baa2e74b522d00ce5d57e8b20471ad2665eaad38e3e
FFMPEG_CORE_SHA256=d00089ce82e1bdf637ddbe42e0c3d41a1ba8cf4c9e825e7fa4d0bb970e844bd4

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dest="${here}/hikvision_recordings/www/vendor"
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

mkdir -p "${dest}"

curl -fsSL -o "${work}/ffmpeg.tgz" \
  "https://registry.npmjs.org/@ffmpeg/ffmpeg/-/ffmpeg-${FFMPEG_WASM_VERSION}.tgz"
curl -fsSL -o "${work}/core.tgz" \
  "https://registry.npmjs.org/@ffmpeg/core/-/core-${FFMPEG_CORE_VERSION}.tgz"

echo "${FFMPEG_WASM_SHA256}  ${work}/ffmpeg.tgz" | sha256sum -c -
echo "${FFMPEG_CORE_SHA256}  ${work}/core.tgz" | sha256sum -c -

tar xzf "${work}/ffmpeg.tgz" -C "${work}" \
  package/dist/umd/ffmpeg.js package/dist/umd/814.ffmpeg.js
tar xzf "${work}/core.tgz" -C "${work}" \
  package/dist/umd/ffmpeg-core.js package/dist/umd/ffmpeg-core.wasm

# 814.ffmpeg.js is the worker the UMD bundle loads by RELATIVE path from wherever
# ffmpeg.js was served, so both must land side by side.
cp "${work}/package/dist/umd/ffmpeg.js" \
   "${work}/package/dist/umd/814.ffmpeg.js" \
   "${work}/package/dist/umd/ffmpeg-core.js" \
   "${work}/package/dist/umd/ffmpeg-core.wasm" \
   "${dest}/"

echo "vendored ffmpeg.wasm ${FFMPEG_WASM_VERSION} + core ${FFMPEG_CORE_VERSION} -> ${dest}"
