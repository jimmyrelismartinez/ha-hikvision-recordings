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
#
# This remains the floor — the budget for reaching frame 0. Reaching a frame
# further into the clip needs proportionally more bytes; see _max_bytes_for().
THUMB_MAX_BYTES = 1024 * 1024

# ── WHY THE PREVIEW IS NOT FRAME 0 (2026-07-27) ──────────────────────────────
# A motion-triggered recording starts slightly BEFORE the event that triggered it,
# so frame 0 is usually an empty scene — a driveway with no car, a room with no
# person. It is a technically valid thumbnail that tells the user nothing. Seeking
# ~15 s in lands on the actual subject far more often, which is the whole point of
# having a preview in the list.
THUMB_TARGET_S = 15.0

# Clips as short as 1 s exist on this DVR, so the target must be clamped rather
# than assumed reachable. For anything shorter than the target we take the
# midpoint: still past the pre-roll, still guaranteed to be inside the clip.
# A clip at or below this length is treated as "just grab frame 0" — seeking
# fractions of a second buys nothing and risks landing past the only keyframe.
THUMB_MIN_SEEK_S = 2.0

# Observed CBR bitrates on this DVR: mainstream 6144 kbps, substream 3072 kbps.
# The byte budget is sized for the MAINSTREAM (the worst case) so a mainstream
# thumbnail is not silently truncated — a substream clip simply reaches the target
# sooner and the read stops early anyway.
THUMB_BITRATE_BPS = 6_144_000
# Headroom past the target so the decoder has a full GOP to work with after the
# seek, rather than stopping on the exact byte the target time lands on.
THUMB_SEEK_MARGIN_BYTES = 2 * 1024 * 1024

# 480px wide keeps a list of 40 rows around 450 KB total instead of ~5 MB at full
# 1920x1080. -2 keeps the aspect ratio and an even height (required by some encoders).
THUMB_WIDTH = 480
THUMB_QUALITY = 5           # ffmpeg -q:v, 2=best..31=worst; 5 ~= 11 KB per frame here
FFMPEG_TIMEOUT_S = 20.0


def thumbnail_offset_for(duration_s: float | int | None) -> float:
    """Seconds into the clip to grab the preview frame from.

    Never returns a value at or past the end of the clip — that is what makes a
    1 s recording still produce a picture instead of an error.
    """
    if not duration_s or duration_s <= 0:
        # Duration unknown: don't gamble on a seek that may be past the end.
        return 0.0
    if duration_s <= THUMB_MIN_SEEK_S:
        return 0.0
    if duration_s < THUMB_TARGET_S:
        return duration_s / 2.0
    return THUMB_TARGET_S


def _max_bytes_for(offset_s: float) -> int:
    """How many bytes must be read to reach `offset_s` at the worst-case bitrate.

    At offset 0 there is nothing to seek past, so this stays at the measured
    frame-0 budget — a clip with an unknown or sub-second duration must not start
    pulling seek-sized reads off the DVR for a frame it was going to take anyway.
    """
    if offset_s <= 0:
        return THUMB_MAX_BYTES
    needed = int(offset_s * THUMB_BITRATE_BPS / 8) + THUMB_SEEK_MARGIN_BYTES
    return max(THUMB_MAX_BYTES, needed)


def ffmpeg_thumb_args(
    width: int = THUMB_WIDTH,
    quality: int = THUMB_QUALITY,
    offset_s: float = 0.0,
) -> list[str]:
    """ffmpeg argv for one JPEG frame, optionally seeking into the stream first.

    `-ss` goes BEFORE `-i` on purpose. Input-side seeking on a pipe cannot actually
    seek, so ffmpeg decodes and discards until the target — which is exactly what we
    want here: it starts from the nearest preceding keyframe and needs no second pass.
    Placing it after `-i` would decode-and-output from frame 0 instead.
    """
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if offset_s > 0:
        args += ["-ss", f"{offset_s:.3f}"]
    args += [
        "-i", "pipe:0",
        "-vframes", "1",
        "-vf", f"scale={width}:-2",
        "-f", "image2",
        "-vcodec", "mjpeg",
        "-q:v", str(quality),
        "pipe:1",
    ]
    return args


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


async def _decode_frame(payload: bytes, offset_s: float) -> bytes:
    """Run ffmpeg over `payload` and return one JPEG, or raise ThumbnailError."""
    process = await asyncio.create_subprocess_exec(
        *ffmpeg_thumb_args(offset_s=offset_s),
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
        LOG.warning(
            "thumbnail decode failed at offset %.3fs (rc=%s): %s",
            offset_s, process.returncode, detail,
        )
        raise ThumbnailError(detail or "ffmpeg produced no frame")
    return stdout


async def thumbnail_from_stream(
    source: AsyncIterator[bytes],
    duration_s: float | int | None = None,
    max_bytes: int | None = None,
) -> bytes:
    """Return JPEG bytes for a preview frame of `source`.

    Seeks THUMB_TARGET_S into the clip (clamped for short ones — see
    thumbnail_offset_for) because frame 0 of a motion-triggered recording is
    usually the empty scene just before the event.

    If that seek yields nothing the frame is retried at offset 0 using the SAME
    bytes — no second DVR fetch. That retry is what guarantees an odd or very short
    clip still gets a real picture rather than a broken-image placeholder.

    Raises ThumbnailError only if both attempts fail — a corrupt clip must surface
    as a placeholder on one row, never as a hung request.
    """
    offset_s = thumbnail_offset_for(duration_s)
    if max_bytes is None:
        max_bytes = _max_bytes_for(offset_s)

    payload = await _take(source, max_bytes)
    if not payload:
        raise ThumbnailError("the DVR returned no data for this clip")

    try:
        return await _decode_frame(payload, offset_s)
    except ThumbnailError:
        if offset_s <= 0:
            raise
        # The clip was shorter than its metadata claimed, the seek landed past the
        # last keyframe, or the read stopped short of the target. Frame 0 is always
        # reachable in the bytes we already hold.
        LOG.info(
            "no frame at %.3fs — falling back to the first frame of this clip",
            offset_s,
        )
        return await _decode_frame(payload, 0.0)
