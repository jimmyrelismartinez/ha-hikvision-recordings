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
slice of the clip, decode from it, and abandon the DVR connection.

Nothing is written to disk: the JPEG is a small in-memory buffer.
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

LOG = logging.getLogger(__name__)

# ── WHY THE LAST FRAME IN THE BUDGET, NOT THE FIRST — AND NOT A FIXED TIME ───
# Frame 0 of a motion-triggered recording is usually the empty scene just before
# whatever triggered it: an empty driveway, an empty room. A later frame is much
# more likely to show the actual subject.
#
# v0.1.4 chased that by seeking to a fixed t=15 s. That was REVERTED (Ramon, live
# on-device, 2026-07-27): reaching 15 s needs ~13.5 MB at this DVR's bitrates, and
# per-thumbnail time went ~0.5 s -> 1.2-2.3 s. Since previews share the DVR's
# max_concurrent_downloads budget (default 2), each one held a slot ~4x longer and
# scrolling the list visibly stalled — rows sat on the placeholder.
#
# So the target is no longer a time at all. We read the SAME cheap slice as before
# and take the LAST frame that slice happens to contain — "as late as the budget
# allows" rather than "as late as some fixed clock target demands". The budget
# caps the cost; the footage decides how far into the clip that reaches.
#
# MEASURED on a real 37 s mainstream clip (6144 kbps), decoding every frame in the
# slice and counting complete JPEGs:
#     1 MB -> 11 frames, ~1.37 s of video
#     2 MB -> 22 frames, ~2.73 s of video
#     4 MB -> 55 frames, ~5.46 s of video
# Every frame came out complete (all ended with the JPEG EOI marker) and visually
# clean — the truncation smearing that plagues a too-small FIRST-frame read did not
# reappear at the tail, because by 1 MB there are already whole GOPs to decode.
# 2 MB is the chosen budget: it doubles the reachable timestamp over the old 1 MB
# for a slice still ~6.75x smaller than the reverted 15 s version needed.
#
# Substreams benefit automatically and for free: at half the bitrate the same 2 MB
# reaches roughly twice as far into the clip. That self-scaling is the main reason
# this is expressed as a byte budget instead of a time target.
THUMB_MAX_BYTES = 2 * 1024 * 1024

# ── THE FIRST-FRAME TRAP — STILL TRUE, DO NOT SHRINK THE BUDGET ──────────────
# Truncating a real 32 MB DVR clip and decoding frame 1 at several cut points:
#     128 KB -> ffmpeg exits 0 and emits a JPEG, but only the top ~25% of the
#               image is decoded; the rest is smeared garbage. It does NOT error.
#     256 KB -> visually complete
#     512 KB -> visually complete, and byte-identical to the 1 MB and 2 MB results
# The dangerous part is that too-small a read produces a *successful* ffmpeg exit
# with a corrupt picture, so this cannot be tuned down by watching return codes —
# only by looking at the image. Do not shrink THUMB_MAX_BYTES to "optimise"
# without re-doing that visual check.

# 480px wide keeps a list of 40 rows around 450 KB total instead of ~5 MB at full
# 1920x1080. -2 keeps the aspect ratio and an even height (required by some encoders).
THUMB_WIDTH = 480
THUMB_QUALITY = 5           # ffmpeg -q:v, 2=best..31=worst; 5 ~= 14 KB per frame here
FFMPEG_TIMEOUT_S = 20.0

JPEG_SOI = b"\xff\xd8\xff"
JPEG_EOI = b"\xff\xd9"


def ffmpeg_thumb_args(width: int = THUMB_WIDTH, quality: int = THUMB_QUALITY) -> list[str]:
    """ffmpeg argv that emits EVERY decodable frame of the slice as concatenated JPEGs.

    image2pipe rather than a single -vframes 1: we want the last frame the slice
    contains, and the slice length is what bounds the cost. Decoding ~20 frames of
    already-fetched bytes is trivial next to the DVR round-trip, and it keeps the
    whole thing in memory — no temp file to overwrite, no second ffmpeg pass, and
    no seek that could land past the end of a short clip.
    """
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-i", "pipe:0",
        "-vf", f"scale={width}:-2",
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "-q:v", str(quality),
        "pipe:1",
    ]


class ThumbnailError(Exception):
    """No decodable frame could be produced from the bytes we read."""


def split_jpegs(blob: bytes) -> list[bytes]:
    """Split concatenated JPEGs, keeping only the complete ones.

    The read is deliberately truncated mid-clip, so the final image can be cut off
    part-way. Requiring the EOI marker drops such a fragment rather than handing a
    half-written picture to the browser.
    """
    frames: list[bytes] = []
    start = blob.find(JPEG_SOI)
    while start != -1:
        nxt = blob.find(JPEG_SOI, start + len(JPEG_SOI))
        frame = blob[start:nxt] if nxt != -1 else blob[start:]
        if frame.endswith(JPEG_EOI):
            frames.append(frame)
        start = nxt
    return frames


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
    """Return JPEG bytes for the latest frame reachable within the read budget.

    Falls back to the first frame implicitly rather than by a second pass: if the
    slice yields only one decodable frame, that one frame is both the first and the
    last, so a very short or awkward clip still gets a real picture. Nothing here
    seeks, so nothing can land past the end of a clip.

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

    frames = split_jpegs(stdout)
    # A truncated read can still exit 0 (see the first-frame trap above), so the
    # real check is whether any COMPLETE frame came out, not the return code.
    if not frames:
        detail = stderr.decode("utf-8", "replace").strip()[:300]
        LOG.warning("thumbnail decode failed (rc=%s): %s", process.returncode, detail)
        raise ThumbnailError(detail or "ffmpeg produced no frame")

    LOG.debug("thumbnail: %d frames decoded from %d bytes", len(frames), len(payload))
    return frames[-1]
