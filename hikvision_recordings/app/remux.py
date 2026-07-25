"""Repackage the DVR's byte stream into something a browser can play.

The DVR's ContentMgmt/download returns Hikvision MPEG-PS (files start with the
magic 'IMKH'), which no browser and no iOS device can play in a <video> element.
We repackage — stream copy, no re-encode, negligible CPU — into fragmented MP4
as the bytes flow through. Nothing is written to disk at any point.
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

LOG = logging.getLogger(__name__)

READ_CHUNK = 65536

# ── VERIFIED FLAGS — DO NOT EDIT WITHOUT RE-TESTING AGAINST A REAL DVR CLIP ──
# `frag_keyframe+empty_moov` puts a valid ftyp+moov at byte 0, so the browser can
# start rendering while the DVR is still sending. Measured 2026-07-24: first byte
# out 0.29 s after input started, on a real 32 MB clip.
# `default_base_is_moof` is not a valid ffmpeg 6.1 movflag (the correctly spelled
# flag is `default_base_moof`), so ffmpeg's option parser rejects it outright,
# before muxing even starts — on ANY input, not just DVR clips — with
#   "Could not write header (incorrect codec parameters ?): Invalid argument"
# It is not an improvement. Leave the list exactly as-is.
# ────────────────────────────────────────────────────────────────────────────
FFMPEG_ARGS = [
    "ffmpeg",
    "-hide_banner",
    "-loglevel", "error",
    "-i", "pipe:0",
    "-c", "copy",
    "-movflags", "frag_keyframe+empty_moov",
    "-f", "mp4",
    "pipe:1",
]


class RemuxError(Exception):
    """ffmpeg could not repackage this stream."""


async def remux_to_fmp4(source: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    process = await asyncio.create_subprocess_exec(
        *FFMPEG_ARGS,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def feed() -> None:
        try:
            async for chunk in source:
                process.stdin.write(chunk)
                await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # ffmpeg exited early; the stdout drain below reports why
        except asyncio.CancelledError:
            raise
        finally:
            if process.stdin and not process.stdin.is_closing():
                try:
                    process.stdin.close()
                except (BrokenPipeError, ConnectionResetError):
                    pass

    feeder = asyncio.create_task(feed())
    produced = 0
    stderr = b""
    source_error: BaseException | None = None
    try:
        while True:
            chunk = await process.stdout.read(READ_CHUNK)
            if not chunk:
                break
            produced += len(chunk)
            yield chunk
    finally:
        # Cleanup ONLY. Client disconnected, stream finished, or we errored — in every
        # case kill ffmpeg and stop pulling from the DVR so its session slot is released.
        # Nothing here may raise: this block also runs on GeneratorExit when the client
        # goes away mid-clip, and raising during GeneratorExit turns a normal disconnect
        # into a RuntimeError.
        feeder.cancel()
        try:
            await feeder
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 — stash it, re-raise below (never here)
            # `source` (e.g. IsapiClient.stream_download) died before producing any
            # bytes — its DvrError is the real cause, not a generic remux failure.
            # Re-raising here would happen inside a `finally` that also runs under
            # GeneratorExit, which is forbidden (see comment above), so we carry it
            # to the normal-completion path below instead.
            source_error = exc
        if process.returncode is None:
            process.kill()
        stderr = await process.stderr.read()
        await process.wait()
        if process.returncode not in (0, -9) and stderr:
            LOG.warning(
                "ffmpeg exited %s: %s",
                process.returncode,
                stderr.decode("utf-8", "replace").strip()[:500],
            )

    # Normal-completion path: reached only when the loop above ended on its own, so
    # raising here is safe (unlike inside `finally`).
    if produced == 0:
        if source_error is not None:
            raise source_error
        detail = stderr.decode("utf-8", "replace").strip()[:500]
        LOG.error("ffmpeg produced no output: %s", detail)
        raise RemuxError(f"Couldn't prepare this clip for playback: {detail}")
