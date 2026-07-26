"""Single-frame JPEG previews for the recordings list.

There is no ISAPI shortcut for this. The search response carries no picture field
on this DVR, and the "channel + 300 = photo track" convention that HikLoad,
hikvision-download-assistant and qb60/hikvision-downloader all rely on (trackID
103 for channel 101) is rejected by this firmware with statusCode 4 / notSupport.
RTSP playback 400s. So the only source of pixels is the same
POST /ISAPI/ContentMgmt/download the video endpoints already use.

The trick is that a thumbnail does NOT need the fragmented-MP4 remux the playback
path performs — that exists only to make a whole clip seekable in a browser. For
one frame, ffmpeg decodes the raw MPEG-PS directly, so we pull only the first
slice of the clip, decode a single frame, and abandon the DVR connection.

Nothing is written to disk: the JPEG is a small in-memory buffer.
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

LOG = logging.getLogger(__name__)

# ── HOW THIS CAP WAS CHOSEN — MEASURED, NOT GUESSED (2026-07-25) ─────────────
# Truncating a real 32 MB DVR clip and decoding frame 1 at several cut points:
#     128 KB -> ffmpeg exits 0 and emits a JPEG, but only the top ~25% of the
#               image is decoded; the rest is smeared garbage. It does NOT error.
#     256 KB -> visually complete
#     512 KB -> visually complete, and byte-identical to the 1 MB and 2 MB results
#   1-2 MB   -> identical to 512 KB
# The dangerous part is that too-small a read produces a *successful* ffmpeg exit
# with a corrupt picture, so this cannot be tuned down by watching return codes —
# only by looking at the image. 1 MB is 2x the observed clean threshold, which
# leaves headroom for a channel with a longer GOP or higher bitrate, while still
# being a small fraction of a typical 20-60 MB clip. Do not shrink this to
# "optimise" without re-doing the visual check.
THUMB_MAX_BYTES = 1024 * 1024

# 480px wide keeps a list of 40 rows around 450 KB total instead of ~5 MB at full
# 1920x1080. -2 keeps the aspect ratio and an even height (required by some encoders).
THUMB_WIDTH = 480
THUMB_QUALITY = 5           # ffmpeg -q:v, 2=best..31=worst; 5 ~= 11 KB per frame here
FFMPEG_TIMEOUT_S = 20.0


def ffmpeg_thumb_args(width: int = THUMB_WIDTH, quality: int = THUMB_QUALITY) -> list[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-i", "pipe:0",
        "-vframes", "1",
        "-vf", f"scale={width}:-2",
        "-f", "image2",
        "-vcodec", "mjpeg",
        "-q:v", str(quality),
        "pipe:1",
    ]


class ThumbnailError(Exception):
    """No decodable frame could be produced from the bytes we read."""


async def _take(source: AsyncIterator[bytes], max_bytes: int) -> bytes:
    """Read up to `max_bytes` from the DVR, then let go of the connection.

    Closing the source is what returns the DVR session permit held by
    stream_download's semaphore — thumbnails share the same limited connection
    budget as playback and downloads, so holding one open would starve them.
    """
    chunks: list[bytes] = []
    taken = 0
    iterator = source.__aiter__()
    try:
        async for chunk in iterator:
            chunks.append(chunk)
            taken += len(chunk)
            if taken >= max_bytes:
                break
    finally:
        await iterator.aclose()
    return b"".join(chunks)[:max_bytes]


async def thumbnail_from_stream(
    source: AsyncIterator[bytes],
    max_bytes: int = THUMB_MAX_BYTES,
) -> bytes:
    """Return JPEG bytes for the first decodable frame of `source`.

    Raises ThumbnailError if ffmpeg produced nothing usable — a corrupt clip must
    surface as a broken-image placeholder on one row, never as a hung request.
    """
    payload = await _take(source, max_bytes)
    if not payload:
        raise ThumbnailError("the DVR returned no data for this clip")

    process = await asyncio.create_subprocess_exec(
        *ffmpeg_thumb_args(),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(payload), timeout=FFMPEG_TIMEOUT_S
        )
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise ThumbnailError("timed out decoding a frame") from exc

    # A truncated read can still exit 0 (see the cap comment above), so also
    # require the JPEG magic — a non-image body must not reach the browser.
    if process.returncode != 0 or not stdout.startswith(b"\xff\xd8\xff"):
        detail = stderr.decode("utf-8", "replace").strip()[:300]
        LOG.warning("thumbnail decode failed (rc=%s): %s", process.returncode, detail)
        raise ThumbnailError(detail or "ffmpeg produced no frame")
    return stdout
