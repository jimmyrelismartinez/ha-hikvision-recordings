"""FastAPI app served through Home Assistant Ingress.

HA authenticates the user before anything reaches us, so there is no login here.
Every response that carries video is a StreamingResponse — the add-on never
stores a byte of it.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from .config import Config, load_config_from_file
from .isapi import (
    DvrAuthError,
    DvrBadRequest,
    DvrBusy,
    DvrError,
    DvrUnreachable,
    IsapiClient,
    Recording,
)
from .registry import ClipExpired, ClipRegistry, InvalidPlaybackUri
from .remux import RemuxError, remux_to_fmp4
from .thumbnail import ThumbnailError, thumbnail_from_stream

VERSION = "0.1.3"
LOG = logging.getLogger(__name__)
WWW_DIR = Path(os.environ.get("ADDON_WWW_DIR", "/www"))

# Where clips are staged for Range-capable playback. config.yaml sets `tmpfs: true`
# so this is RAM inside the add-on container, never the Pi's disk.
STAGING_DIR = Path(os.environ.get("ADDON_STAGING_DIR", "/tmp"))
STAGE_PREFIX = "hikclip-"
STAGE_MAX_AGE_S = 900.0   # a staged file older than this is orphaned; sweep it


def _discard_stage(path: Path) -> None:
    """Delete one staged clip. Runs as a BackgroundTask once the response is done."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:  # pragma: no cover - defensive
        LOG.warning("could not remove staged clip %s: %s", path, exc)


def _sweep_stale_stages() -> None:
    """Remove staged clips a previous request failed to clean up.

    Belt-and-braces for the zero-persistence guarantee: if the process is killed
    mid-response its BackgroundTask never runs, and without this the next restart
    would inherit that file. Cheap - one directory listing per staging call.
    """
    now = time.time()
    try:
        for leftover in STAGING_DIR.glob(f"{STAGE_PREFIX}*"):
            try:
                if now - leftover.stat().st_mtime > STAGE_MAX_AGE_S:
                    leftover.unlink(missing_ok=True)
                    LOG.warning("swept orphaned staged clip %s", leftover)
            except OSError:
                continue
    except OSError:  # pragma: no cover - staging dir missing
        pass

ERROR_STATUS = {
    DvrAuthError: 401,
    DvrUnreachable: 502,
    DvrBadRequest: 502,
    DvrBusy: 503,
}


def _http_error(exc: DvrError) -> HTTPException:
    for kind, status in ERROR_STATUS.items():
        if isinstance(exc, kind):
            return HTTPException(status_code=status, detail=str(exc))
    return HTTPException(status_code=502, detail=str(exc))


def _parse_utc(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"{field} is not a valid ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_filename(channel_name: str, recording: Recording, extension: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", channel_name).strip("_") or "camera"
    stamp = recording.start.strftime("%Y-%m-%d_%H-%M-%S")
    return f"{slug}_{stamp}{extension}"


def create_app(config: Config, client: IsapiClient | None = None) -> FastAPI:
    names = {channel.id: channel.name for channel in config.channels}
    registry = ClipRegistry(dvr_host=config.dvr_host)
    max_stage_bytes = config.max_stage_mb * 1024 * 1024
    dvr = client or IsapiClient(config)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            await dvr.probe_clock()
        except Exception as exc:  # noqa: BLE001 — never block startup on the DVR
            LOG.warning("clock probe failed at startup: %s", exc)
        yield
        await dvr.aclose()

    app = FastAPI(title="DVR Recordings", version=VERSION, lifespan=lifespan)

    @app.get("/api/channels")
    async def channels():
        return [{"id": c.id, "name": c.name} for c in config.channels]

    @app.get("/api/health")
    async def health():
        state = "ok"
        try:
            # A ping, not a search: a zero-width timeSpan has unknown firmware
            # behaviour and could make a healthy DVR look unreachable.
            await dvr.ping()
        except DvrAuthError:
            state = "auth_failed"
        except DvrError:
            state = "unreachable"
        return {
            "dvr": state,
            # Offset actually applied to queries, plus how far the device's clock sits
            # from a whole-hour zone (i.e. the part that is a wrong clock, not a zone).
            "clock_offset_s": int(dvr.clock.offset.total_seconds()),
            "clock_source": dvr.clock.source,
            "clock_drift_s": int(getattr(dvr.clock, "drift", timedelta(0)).total_seconds()),
            "version": VERSION,
        }

    @app.get("/api/recordings")
    async def recordings(
        channel: int = Query(...),
        start: str = Query(...),
        end: str = Query(...),
    ):
        if channel not in names:
            raise HTTPException(status_code=400, detail="Unknown channel.")
        start_utc = _parse_utc(start, "start")
        end_utc = _parse_utc(end, "end")
        if end_utc <= start_utc:
            raise HTTPException(status_code=400, detail="The end time must be after the start time.")
        try:
            found = await dvr.search(channel, start_utc, end_utc)
        except DvrError as exc:
            raise _http_error(exc) from exc

        clips = []
        for recording in found:
            try:
                clip_id = registry.put(recording)
            except InvalidPlaybackUri:
                LOG.warning("dropping a search result with an unexpected playbackURI host")
                continue
            clips.append(
                {
                    "id": clip_id,
                    "channel": recording.channel,
                    "channel_name": names[recording.channel],
                    "start": recording.start.isoformat().replace("+00:00", "Z"),
                    "end": recording.end.isoformat().replace("+00:00", "Z"),
                    "duration_s": recording.duration_s,
                    "size_bytes": recording.size_bytes,
                }
            )
        return {"clips": clips, "truncated": len(found) >= config.max_results}

    def _lookup(clip_id: str) -> Recording:
        try:
            return registry.get(clip_id)
        except ClipExpired as exc:
            raise HTTPException(
                status_code=410, detail="That result expired — search again."
            ) from exc

    async def _open_video_stream(recording: Recording, raw: bool, request: Request):
        """Start the pipeline and pull the first chunk *before* the response begins.

        Once StreamingResponse has been constructed the status line is already sent,
        so a later failure can only produce a 200 with zero bytes and a silently dead
        <video>. Priming here is what lets DVR errors map to real 502/401/503 codes.

        CARRY-FORWARD (Task 5 review): stream_download()'s `finally: sem.release()`
        only fires on exhaustion, an explicit aclose(), or GC — never on a bare
        `break` out of an `async for`. A client that disconnects mid-download would
        otherwise leak a DVR session-cap permit until GC. So both here (on an error
        priming the first chunk) and in body()'s finally (on disconnect, exhaustion,
        or any other exit) we explicitly aclose() the top-level pipeline iterator,
        which propagates GeneratorExit down through remux/stream_download and
        releases the semaphore immediately instead of waiting for garbage collection.
        """
        source = dvr.stream_download(recording.playback_uri)
        pipeline = source if raw else remux_to_fmp4(source)
        iterator = pipeline.__aiter__()
        try:
            first = await iterator.__anext__()
        except StopAsyncIteration:
            first = b""
        except DvrError as exc:
            await iterator.aclose()
            raise _http_error(exc) from exc
        except RemuxError as exc:
            await iterator.aclose()
            raise HTTPException(
                status_code=500, detail="Couldn't prepare this clip for playback."
            ) from exc

        async def body():
            try:
                if first:
                    yield first
                try:
                    async for chunk in iterator:
                        if await request.is_disconnected():
                            break
                        yield chunk
                except DvrError as exc:
                    LOG.warning("DVR failed mid-stream: %s", exc)
                except RemuxError as exc:
                    LOG.error("remux failed mid-stream: %s", exc)
            finally:
                # Prompt semaphore release — see CARRY-FORWARD note above. Safe to
                # call even if `iterator` is already exhausted or was never started.
                await iterator.aclose()

        return body()

    # ── SUBSTREAM PLAYBACK ───────────────────────────────────────────────────
    # Hikvision track numbering is channel*100 + streamType, where 1=main, 2=sub
    # and 3=photo/snapshot. So the substream of configured channel 101 is 102.
    # (streamType 3 is NOT supported on this DVR — statusCode 4 / notSupport —
    # which is why thumbnails decode a frame out of the video instead.)
    #
    # Verified live on this DVR: the substream is independently recorded at
    # 960x480 CBR 3072k vs the mainstream's 1920x1080 CBR 6144k. For the same 35 s
    # clip, mainstream was 17.8 MB / 7.4 s to fetch; substream 10.5 MB / 3.4 s —
    # better than the size ratio alone predicts, so the DVR serves it more
    # efficiently too. Since the Range fix means a clip must be fully fetched and
    # remuxed before playback can start, that roughly halves time-to-play.
    #
    # ⚠️ FIRMWARE QUIRK — DO NOT "FIX" THIS: a search on trackID 102 returns
    # results whose <trackID> element says "101". The label is wrong; the footage
    # really is the substream (confirmed with ffprobe: 960x480). parse_search_response
    # deliberately records the channel we ASKED for and ignores that element, so
    # nothing here trusts the mislabel. Do not add code that reads it.
    SUBSTREAM_STREAM_TYPE_OFFSET = 1
    # Main and sub recordings for the same event start/stop at slightly different
    # instants, so widen the window before matching by overlap.
    SUBSTREAM_WINDOW_PAD = timedelta(seconds=30)

    async def _substream_for(recording: Recording) -> Recording | None:
        """Find the substream counterpart of a mainstream clip, or None.

        Returning None is a normal outcome, not a failure: a channel may have had
        substream recording disabled, or this particular window may exist only on
        the mainstream. The caller falls back to the mainstream so playback still
        works — just slower.
        """
        sub_channel = recording.channel + SUBSTREAM_STREAM_TYPE_OFFSET
        try:
            candidates = await dvr.search(
                sub_channel,
                recording.start - SUBSTREAM_WINDOW_PAD,
                recording.end + SUBSTREAM_WINDOW_PAD,
            )
        except DvrError as exc:
            LOG.warning("substream search failed for channel %s (%s)", sub_channel, exc)
            return None

        # Pick the candidate overlapping the mainstream clip the most — a padded
        # window can legitimately return the neighbouring event as well.
        best: Recording | None = None
        best_overlap = timedelta(0)
        for candidate in candidates:
            overlap = min(candidate.end, recording.end) - max(candidate.start, recording.start)
            if overlap > best_overlap:
                best, best_overlap = candidate, overlap
        if best is None:
            return None
        try:
            # Same SSRF check the registry applies to mainstream URIs.
            registry.validate_uri(best.playback_uri)
        except InvalidPlaybackUri:
            LOG.warning("substream playbackURI failed validation; using mainstream")
            return None
        return best

    @app.get("/api/stream/{clip_id}")
    async def stream(clip_id: str, request: Request):
        recording = _lookup(clip_id)
        # Play the substream: it is a smaller, independently recorded copy of the
        # same event, and since the Range fix requires staging the whole clip
        # before playback starts, halving the bytes roughly halves time-to-play.
        # Download deliberately stays on the mainstream for full quality.
        playback = await _substream_for(recording)
        if playback is None:
            LOG.info(
                "no substream for channel %s at %s — falling back to the mainstream",
                recording.channel, recording.start.isoformat(),
            )
            playback = recording
        # Staged to a RAM-backed file rather than streamed, so the response carries
        # Content-Length and answers Range — without which iOS refuses to play at
        # all. See _stage_clip() for the full reasoning and the cleanup guarantees.
        path = await _stage_clip(playback, raw=False)
        return FileResponse(
            path,
            media_type="video/mp4",
            headers={"Cache-Control": "no-store"},
            background=BackgroundTask(_discard_stage, path),
        )

    async def _stage_clip(recording: Recording, raw: bool) -> Path:
        """Write the remuxed clip to a RAM-backed temp file and return its path.

        WHY THIS EXISTS — iOS, verified live 2026-07-25 (spec section 7):
        A chunked StreamingResponse carries no Content-Length and cannot answer a
        Range request. iOS Safari / the HA Companion WKWebView refuse to initialise
        <video> against such a response — Ramon got the crossed-out "media
        unsupported" icon even though the bytes were a perfectly valid H.264 MP4
        (ffprobe-confirmed, and Download of the very same clip worked). Staging to a
        real file lets Starlette's FileResponse answer with Content-Length +
        Accept-Ranges: bytes and serve 206 slices, which is what WebKit needs.

        ZERO-PERSISTENCE IS STILL HONOURED, and this is the one place it could be
        broken, so it is enforced three ways:
          1. The file lives under STAGING_DIR (/tmp), which config.yaml declares as
             `tmpfs: true` — RAM, never the Pi's SD card / disk.
          2. It is unlinked by a BackgroundTask the moment the response completes.
          3. _sweep_stale_stages() removes anything a missed cleanup left behind, so
             a crash mid-response cannot accumulate clips.
        Nothing survives a container restart, and nothing is ever written to the
        add-on's persistent /data.
        """
        _sweep_stale_stages()
        source = dvr.stream_download(recording.playback_uri)
        pipeline = source if raw else remux_to_fmp4(source)
        iterator = pipeline.__aiter__()

        fd, path_str = tempfile.mkstemp(
            prefix="hikclip-", suffix=".mp4", dir=str(STAGING_DIR)
        )
        path = Path(path_str)
        written = 0
        try:
            with os.fdopen(fd, "wb") as handle:
                async for chunk in iterator:
                    written += len(chunk)
                    if written > max_stage_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=(
                                "That clip is too large to prepare for in-browser "
                                f"playback (over {max_stage_bytes // (1024 * 1024)} MB). "
                                "Use Download instead."
                            ),
                        )
                    handle.write(chunk)
        except DvrError as exc:
            path.unlink(missing_ok=True)
            raise _http_error(exc) from exc
        except RemuxError as exc:
            path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=500, detail="Couldn't prepare this clip for playback."
            ) from exc
        except BaseException:
            # Includes the 413 above and client-disconnect cancellation.
            path.unlink(missing_ok=True)
            raise
        finally:
            # Release the DVR session permit immediately rather than at GC — same
            # reasoning as the streaming path's aclose().
            await iterator.aclose()

        if written == 0:
            path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=502, detail="The DVR returned an empty clip."
            )
        return path

    @app.get("/api/download/{clip_id}")
    async def download(clip_id: str, request: Request, raw: int = 0):
        recording = _lookup(clip_id)
        extension = ".mpg" if raw else ".mp4"
        media_type = "video/mpeg" if raw else "video/mp4"
        filename = _safe_filename(names.get(recording.channel, "camera"), recording, extension)
        body = await _open_video_stream(recording, raw=bool(raw), request=request)
        return StreamingResponse(
            body,
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )

    @app.get("/api/thumbnail/{clip_id}")
    async def thumbnail(clip_id: str):
        """One JPEG frame for a clip, for the results list.

        Deliberately goes through dvr.stream_download() like playback and download
        do, so thumbnails contend for the SAME max_concurrent_downloads budget
        rather than opening an unlimited side channel to the DVR — a list of 40
        rows could otherwise swamp it. A busy DVR therefore returns 503 here too,
        by the same path as the video endpoints.
        """
        recording = _lookup(clip_id)
        source = dvr.stream_download(recording.playback_uri)
        try:
            jpeg = await thumbnail_from_stream(source)
        except DvrError as exc:
            raise _http_error(exc) from exc
        except ThumbnailError as exc:
            # One unreadable clip must degrade to a placeholder on that row, not
            # break the list — so this is a clean status, not a 500 traceback.
            LOG.info("no preview for clip %s: %s", clip_id, exc)
            raise HTTPException(
                status_code=502, detail="No preview available for this clip."
            ) from exc
        return Response(
            content=jpeg,
            media_type="image/jpeg",
            # Clip ids live as long as the registry entry (1 h), so a browser may
            # safely reuse the image for that long instead of re-hitting the DVR.
            headers={"Cache-Control": "private, max-age=3600"},
        )

    @app.get("/")
    async def index():
        return FileResponse(WWW_DIR / "index.html")

    # Mounted last so it can never shadow the /api routes above.
    app.mount("/", StaticFiles(directory=str(WWW_DIR)), name="static")
    return app


def _build_default_app() -> FastAPI | None:
    try:
        return create_app(load_config_from_file())
    except Exception as exc:  # noqa: BLE001 — importing under pytest must not explode
        LOG.debug("default app not built: %s", exc)
        return None


app = _build_default_app()
