"""FastAPI app served through Home Assistant Ingress.

HA authenticates the user before anything reaches us, so there is no login here.
Every response that carries video is a StreamingResponse — the add-on never
stores a byte of it.
"""
from __future__ import annotations

import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

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

VERSION = "0.1.0"
LOG = logging.getLogger(__name__)
WWW_DIR = Path(os.environ.get("ADDON_WWW_DIR", "/www"))

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
            "clock_offset_s": int(dvr.clock.offset.total_seconds()),
            "clock_source": dvr.clock.source,
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

    @app.get("/api/stream/{clip_id}")
    async def stream(clip_id: str, request: Request):
        recording = _lookup(clip_id)
        body = await _open_video_stream(recording, raw=False, request=request)
        return StreamingResponse(
            body,
            media_type="video/mp4",
            headers={"Cache-Control": "no-store", "Accept-Ranges": "none"},
        )

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
