"""FastAPI app served through Home Assistant Ingress.

HA authenticates the user before anything reaches us, so there is no login here.
Every response that carries video is a StreamingResponse — the add-on never
stores a byte of it.
"""
from __future__ import annotations

import asyncio
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

from .clips import KIND_DVR, KIND_REOLINK, SUB_WINDOW_PAD, Clip, best_overlap
from .config import Config, load_config_from_file
from .ha import HaAuthError, HaBadEntity, HaClient, HaError, HaNotFound, HaUnreachable
from .isapi import (
    ClipGone,
    DvrAuthError,
    DvrBadRequest,
    DvrBusy,
    DvrError,
    DvrUnreachable,
    IsapiClient,
    RangeNotSatisfiable,
    Recording,
)
from .reolink import ReolinkClient
from .registry import ClipExpired, ClipRegistry, InvalidPlaybackUri
from .remux import RemuxError, remux_to_fmp4
from .thumbnail import (
    THUMB_MAX_BYTES,
    ThumbnailCache,
    ThumbnailError,
    thumbnail_from_stream,
)

VERSION = "0.6.0"
LOG = logging.getLogger(__name__)
WWW_DIR = Path(os.environ.get("ADDON_WWW_DIR", "/www"))

# Supervisor ingress proxy + local loopback. Used only when SUPERVISOR_TOKEN was
# present at startup (see IngressOnlyMiddleware).
INGRESS_PEERS = frozenset({"172.30.32.2", "127.0.0.1"})

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

# Playback quality the caller may ask for. "sd" = the DVR's independently recorded
# substream, "hd" = the mainstream. SD is the default because it is roughly half the
# bytes and therefore materially faster to fetch on both playback paths.
# /api/download became quality-aware in v0.2.0 but defaults to "hd", so any caller
# that does not ask still gets the full mainstream, exactly as before.
QUALITIES = ("sd", "hd")
DEFAULT_QUALITY = "sd"
DEFAULT_DOWNLOAD_QUALITY = "hd"


def _check_quality(quality: str) -> None:
    if quality not in QUALITIES:
        raise HTTPException(
            status_code=400, detail="quality must be 'sd' or 'hd'."
        )


# ── STATIC ASSET CACHING — THIS IS A BUG FIX, NOT A TUNING KNOB ──────────────
# Starlette's StaticFiles sends ETag + Last-Modified but NO Cache-Control. With no
# explicit freshness, browsers fall back to HEURISTIC caching (RFC 9111 §4.2.2):
# they invent a lifetime from Last-Modified and reuse the asset WITHOUT
# revalidating. iOS WKWebView — the HA Companion app — is especially eager here.
#
# Verified failure, 2026-07-27: after updating v0.1.3 -> v0.1.4, Ramon's iPhone
# kept executing the CACHED v0.1.3 app.js. api/health correctly reported 0.1.4 (a
# fresh API call), while the UI was the old renderer, which appended only
# <video>/hint/download and had no SD/HD picker and no mode badge — exactly the
# "nothing but native video controls" symptom he reported. The add-on looked
# broken when it was simply never running the shipped frontend.
#
# So: the app's own HTML/JS/CSS must ALWAYS revalidate. `no-cache` does not mean
# "don't store" — it means "revalidate before reuse", so the usual outcome is a
# cheap 304 on an unchanged file, not a re-download.
NO_CACHE = "no-cache"
# The vendored ffmpeg.wasm core is ~32 MB and its content is pinned by version in
# the Dockerfile, so it is safe (and important) to cache hard. A new add-on
# version rebuilds the image and would ship a different file.
VENDOR_CACHE = "public, max-age=31536000, immutable"
VENDOR_PREFIX = "vendor/"


class _CachePolicyStatic(StaticFiles):
    """StaticFiles that states its freshness instead of leaving it to guesswork."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        normalised = path.lstrip("/")
        response.headers["Cache-Control"] = (
            VENDOR_CACHE if normalised.startswith(VENDOR_PREFIX) else NO_CACHE
        )
        return response


ERROR_STATUS = {
    ClipGone: 410,
    RangeNotSatisfiable: 416,
    DvrAuthError: 401,
    DvrUnreachable: 502,
    DvrBadRequest: 502,
    DvrBusy: 503,
}


class IngressOnlyMiddleware:
    """When running under Supervisor, refuse peers that are not the ingress proxy.

    Off when no SUPERVISOR_TOKEN was present (local dev / tests), so TestClient
    and `uvicorn` on a laptop keep working without HA.
    """

    def __init__(self, app, enabled: bool) -> None:
        self.app = app
        self.enabled = enabled

    async def __call__(self, scope, receive, send):
        if self.enabled and scope["type"] == "http":
            client = scope.get("client")
            host = client[0] if client else None
            if host not in INGRESS_PEERS:
                await Response(
                    content='{"detail":"Forbidden"}',
                    status_code=403,
                    media_type="application/json",
                )(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _http_error(exc: DvrError) -> HTTPException:
    for kind, status in ERROR_STATUS.items():
        if isinstance(exc, kind):
            headers = None
            if isinstance(exc, RangeNotSatisfiable) and exc.content_range:
                headers = {"Content-Range": exc.content_range}
            return HTTPException(status_code=status, detail=str(exc), headers=headers)
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


def _safe_filename(channel_name: str, recording: Recording | Clip, extension: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", channel_name).strip("_") or "camera"
    stamp = recording.start.strftime("%Y-%m-%d_%H-%M-%S")
    return f"{slug}_{stamp}{extension}"


def create_app(
    config: Config,
    client: IsapiClient | None = None,
    reolink_clients: list | None = None,
    ha_client: HaClient | None = None,
) -> FastAPI:
    """Build the app. `client` / `reolink_clients` / `ha_client` are test injectors.

    SUPERVISOR_TOKEN is read once here and removed from the environment so ffmpeg
    subprocesses (and anything else) cannot inherit it.
    """
    token = os.environ.pop("SUPERVISOR_TOKEN", None)
    ha = ha_client if ha_client is not None else HaClient(token)
    ingress_only = token is not None

    dvr = (client or IsapiClient(config)) if config.dvr_configured else None
    reolinks = (
        reolink_clients if reolink_clients is not None else [
            ReolinkClient(camera, f"reolink-{index}", config.max_concurrent_downloads)
            for index, camera in enumerate(config.reolink_cameras)
        ]
    )
    reolink_by_key = {rc.key: rc for rc in reolinks}

    # Every camera, DVR channel or Reolink, is addressed by a string key — integer
    # channel ids collide across devices.
    cameras: dict[str, dict] = {}
    if dvr is not None:
        for channel in config.channels:
            cameras[f"dvr-{channel.id}"] = {
                "name": channel.name, "kind": KIND_DVR, "channel": channel.id,
                "ha_camera": channel.ha_camera,
            }
    for rc in reolinks:
        cameras[rc.key] = {
            "name": rc.camera.name, "kind": KIND_REOLINK,
            "channel": rc.camera.channel, "ha_camera": rc.camera.ha_camera,
        }

    # Sized so one all-cameras response can never evict its own newest clips
    # (goldfish B1); entries are also inserted oldest-first below.
    # Floor raised to 2000 so an infinite-scroll day walk cannot FIFO-evict Today
    # (design §10.1); put() also reuses ids for already-known clips.
    registry = ClipRegistry(
        dvr_host=config.dvr_host or None,
        capacity=max(2000, len(cameras) * config.max_search_results * 2),
    )
    thumb_cache = ThumbnailCache()
    max_stage_bytes = config.max_stage_mb * 1024 * 1024
    # One search at a time per DVR: an all-cameras search would otherwise fire
    # 2 searches per channel at a device that answers "busy" to far less.
    dvr_search_sem = asyncio.Semaphore(1)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async def probe(device, label):
            try:
                await device.probe_clock()
            except Exception as exc:  # noqa: BLE001 — never block startup on a device
                LOG.warning("clock probe failed at startup for %s: %s", label, exc)

        if dvr is None:
            LOG.info("DVR not configured (dvr_host is blank) — Reolink cameras only")
        await asyncio.gather(
            *([probe(dvr, "the DVR")] if dvr is not None else []),
            *(probe(rc, rc.camera.name) for rc in reolinks),
        )
        yield
        await asyncio.gather(
            *([dvr.aclose()] if dvr is not None else []),
            *(rc.aclose() for rc in reolinks),
            ha.aclose(),
            return_exceptions=True,
        )

    app = FastAPI(title="DVR Recordings", version=VERSION, lifespan=lifespan)
    app.add_middleware(IngressOnlyMiddleware, enabled=ingress_only)

    @app.get("/api/channels")
    async def channels():
        # `id` is kept (now the key) so a cached pre-0.2 frontend still works: it
        # sends `channel=<id>`, which /api/recordings accepts.
        # `snapshot` is true only when linked AND the HA client has a token.
        return [
            {
                "id": key, "key": key, "name": cam["name"], "kind": cam["kind"],
                "snapshot": bool(cam.get("ha_camera") and ha.enabled),
            }
            for key, cam in cameras.items()
        ]

    async def _device_state(device) -> str:
        try:
            await device.ping()
        except DvrAuthError:
            return "auth_failed"
        except DvrError:
            return "unreachable"
        return "ok"

    @app.get("/api/health")
    async def health():
        devices_list = ([("dvr", KIND_DVR, "DVR", dvr)] if dvr is not None else []) + [
            (rc.key, KIND_REOLINK, rc.camera.name, rc) for rc in reolinks
        ]
        states = await asyncio.gather(*(_device_state(d[3]) for d in devices_list))
        devices = []
        for (prefix, kind, name, device), state in zip(devices_list, states):
            clock = device.clock
            devices.append({
                "prefix": prefix, "kind": kind, "name": name, "state": state,
                "clock_offset_s": int(clock.offset.total_seconds()),
                "clock_source": clock.source,
            })
        primary = dvr if dvr is not None else (reolinks[0] if reolinks else None)
        clock = primary.clock if primary is not None else None
        return {
            "dvr": states[0] if dvr is not None else "not_configured",
            # Offset actually applied to queries, plus how far the device's clock sits
            # from a whole-hour zone (i.e. the part that is a wrong clock, not a zone).
            "clock_offset_s": int(clock.offset.total_seconds()) if clock else 0,
            "clock_source": clock.source if clock else "none",
            "clock_drift_s": int(getattr(clock, "drift", timedelta(0)).total_seconds())
            if clock else 0,
            "devices": devices,
            "version": VERSION,
            "client_remux_max_mb": config.client_remux_max_mb,
            # No upstream call — just whether the Supervisor token was available.
            "ha_api": "ok" if ha.enabled else "off",
        }

    # ── SEARCH ───────────────────────────────────────────────────────────────
    async def _dvr_search_paged(channel: int, start_utc: datetime, end_utc: datetime):
        """Every recording in the window, by TIME CURSOR — not searchResultPosition.

        Verified live 2026-11-14 (verify_dvr_contract.py Q5): this DVR IGNORES
        searchResultPosition (positions 0..140 all return the same first page and
        always say MORE) and caps one search at 64 items whatever maxResults says.
        Results come back oldest first, so the next page is a fresh search starting
        at the last item's start; overlap is deduped by playbackURI. A short page
        ends the walk; a full page that brings nothing new means many clips share
        one start time and the rest are unreachable, so the result is truncated.
        """
        page = max(1, min(config.max_results, 50))
        found: dict[str, Recording] = {}
        cursor = start_utc
        truncated = False
        while True:
            async with dvr_search_sem:
                batch = await dvr.search(channel, cursor, end_utc, page)
            new = [r for r in batch if r.playback_uri not in found]
            for r in new:
                found[r.playback_uri] = r
            if len(found) >= config.max_search_results:
                truncated = len(found) > config.max_search_results or len(batch) >= page
                break
            if len(batch) < page:
                break
            if not new:
                truncated = True
                break
            next_cursor = max(r.start for r in batch)
            if next_cursor <= cursor:
                truncated = True
                break
            cursor = next_cursor
        recordings = sorted(found.values(), key=lambda r: r.start)
        return recordings[: config.max_search_results], truncated

    async def _search_dvr_camera(key: str, start_utc: datetime, end_utc: datetime):
        channel = cameras[key]["channel"]
        mains, truncated = await _dvr_search_paged(channel, start_utc, end_utc)
        # One substream search for the whole window, instead of one per play, so SD
        # playback and thumbnails need no extra DVR search. Padded like the per-play
        # lookup. Failure here only costs the pairing — SD then falls back per play.
        subs: list[Recording] = []
        if mains:
            try:
                subs, _ = await _dvr_search_paged(
                    channel + SUBSTREAM_STREAM_TYPE_OFFSET,
                    start_utc - SUB_WINDOW_PAD, end_utc + SUB_WINDOW_PAD,
                )
            except DvrError as exc:
                LOG.warning("substream search failed for channel %s (%s)", channel, exc)
        clips = []
        for rec in mains:
            sub = best_overlap(rec.start, rec.end, subs)
            clips.append(Clip(
                camera_key=key, kind=KIND_DVR, channel=rec.channel, start=rec.start,
                end=rec.end, size_bytes=rec.size_bytes, main_ref=rec.playback_uri,
                sub_ref=sub.playback_uri if sub else None,
                sub_size_bytes=sub.size_bytes if sub else None,
                events=rec.events,
            ))
        return clips, truncated

    async def _search_camera(key: str, start_utc: datetime, end_utc: datetime):
        if cameras[key]["kind"] == KIND_DVR:
            return await _search_dvr_camera(key, start_utc, end_utc)
        return await reolink_by_key[key].search(start_utc, end_utc, config.max_search_results)

    def _resolve_cameras(requested: list[str]) -> list[str]:
        keys: list[str] = []
        for raw in requested:
            for part in str(raw).split(","):
                part = part.strip()
                if not part:
                    continue
                if part == "all":
                    keys.extend(cameras)
                    continue
                if part.isdigit():          # legacy `channel=101`
                    part = f"dvr-{part}"
                if part not in cameras:
                    raise HTTPException(status_code=400, detail="Unknown camera.")
                keys.append(part)
        if not keys:
            raise HTTPException(status_code=400, detail="Pick at least one camera.")
        return list(dict.fromkeys(keys))

    def _clip_json(clip_id: str, clip: Clip) -> dict:
        name = cameras[clip.camera_key]["name"]
        return {
            "id": clip_id,
            "channel": clip.camera_key,
            "camera_key": clip.camera_key,
            "channel_name": name,
            "camera_name": name,
            "kind": clip.kind,
            # "direct": the device already serves browser-playable MP4 with Range
            # (Reolink) — no remux on either side. "remux": Hikvision MPEG-PS.
            "playback": "direct" if clip.kind == KIND_REOLINK else "remux",
            "start": clip.start.isoformat().replace("+00:00", "Z"),
            "end": clip.end.isoformat().replace("+00:00", "Z"),
            "duration_s": clip.duration_s,
            "size_bytes": clip.size_bytes,
            "has_sub": clip.sub_ref is not None,
            "events": list(clip.events),
        }

    @app.get("/api/snapshot/{camera_key}")
    async def snapshot(camera_key: str):
        """Near-live still from Home Assistant's camera_proxy for a linked camera."""
        cam = cameras.get(camera_key)
        if cam is None:
            raise HTTPException(status_code=404, detail="Unknown camera.")
        entity = cam.get("ha_camera")
        if not entity:
            raise HTTPException(
                status_code=404, detail="Not linked to a Home Assistant camera"
            )
        if not ha.enabled:
            raise HTTPException(
                status_code=503,
                detail="Snapshots need the Home Assistant API (homeassistant_api)",
            )
        try:
            body, content_type = await ha.snapshot(entity)
        except HaNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except HaBadEntity as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (HaUnreachable, HaAuthError, HaError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return Response(
            content=body,
            media_type=content_type,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/api/recordings")
    async def recordings(
        start: str = Query(...),
        end: str = Query(...),
        camera: list[str] | None = Query(None),
        channel: str | None = Query(None),
    ):
        keys = _resolve_cameras(list(camera or []) + ([channel] if channel else []))
        start_utc = _parse_utc(start, "start")
        end_utc = _parse_utc(end, "end")
        if end_utc <= start_utc:
            raise HTTPException(status_code=400, detail="The end time must be after the start time.")

        # Cameras on different devices search concurrently; within a device the
        # search semaphore serialises them. One device being down must not cost
        # the user the others, so DvrErrors become per-camera `errors` entries.
        outcomes = await asyncio.gather(
            *(_search_camera(key, start_utc, end_utc) for key in keys),
            return_exceptions=True,
        )
        found: list[Clip] = []
        errors: list[tuple[str, DvrError]] = []
        truncated_cameras: list[str] = []
        for key, outcome in zip(keys, outcomes):
            if isinstance(outcome, DvrError):
                errors.append((key, outcome))
                continue
            if isinstance(outcome, BaseException):
                raise outcome            # a bug, not a device failure — don't hide it
            clips, truncated = outcome
            found.extend(clips)
            if truncated:
                truncated_cameras.append(key)
        if errors and len(errors) == len(keys):
            raise _http_error(errors[0][1]) from errors[0][1]

        # Register oldest first, so if capacity is ever exceeded the registry evicts
        # the oldest clips of this response, never the newest. Return newest first.
        found.sort(key=lambda c: c.start)
        listed = []
        for clip in found:
            try:
                clip_id = registry.put(clip)
            except InvalidPlaybackUri:
                LOG.warning("dropping a search result with an unexpected ref")
                continue
            listed.append(_clip_json(clip_id, clip))
        listed.reverse()
        return {
            "clips": listed,
            "truncated": bool(truncated_cameras),
            "truncated_cameras": truncated_cameras,
            "errors": [{"camera": key, "detail": str(exc)} for key, exc in errors],
        }

    def _lookup(clip_id: str) -> Clip:
        try:
            return registry.get(clip_id)
        except ClipExpired as exc:
            raise HTTPException(
                status_code=410, detail="That result expired — search again."
            ) from exc

    def _as_recording(clip: Clip, sub: bool = False) -> Recording:
        """The DVR code paths below predate Clip and speak Recording."""
        if sub:
            return Recording(channel=clip.channel + SUBSTREAM_STREAM_TYPE_OFFSET,
                             start=clip.start, end=clip.end,
                             size_bytes=clip.sub_size_bytes or 0, playback_uri=clip.sub_ref)
        return Recording(channel=clip.channel, start=clip.start, end=clip.end,
                         size_bytes=clip.size_bytes, playback_uri=clip.main_ref)

    def _reolink_ref(clip: Clip, quality: str) -> str:
        return clip.sub_ref if quality == "sd" and clip.sub_ref else clip.main_ref

    async def _open_reolink(clip: Clip, ref: str, range_header: str | None, clamp: bool):
        try:
            return await reolink_by_key[clip.camera_key].open(ref, range_header, clamp=clamp)
        except DvrError as exc:
            raise _http_error(exc) from exc

    def _reolink_response(upstream, extra_headers: dict | None = None) -> StreamingResponse:
        headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store", **(extra_headers or {})}
        for name in ("content-length", "content-range"):
            if name in upstream.headers:
                headers[name.title()] = upstream.headers[name]
        return StreamingResponse(
            upstream.body,
            status_code=upstream.status,
            # The firmware labels MP4 as "apolication/octet-stream" (sic).
            media_type="video/mp4",
            headers=headers,
            background=BackgroundTask(upstream.aclose),
        )

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
                recording.start - SUB_WINDOW_PAD,
                recording.end + SUB_WINDOW_PAD,
            )
        except DvrError as exc:
            LOG.warning("substream search failed for channel %s (%s)", sub_channel, exc)
            return None

        # Pick the candidate overlapping the mainstream clip the most — a padded
        # window can legitimately return the neighbouring event as well.
        best = best_overlap(recording.start, recording.end, candidates)
        if best is None:
            return None
        try:
            # Same SSRF check the registry applies to mainstream URIs.
            registry.validate_uri(best.playback_uri)
        except InvalidPlaybackUri:
            LOG.warning("substream playbackURI failed validation; using mainstream")
            return None
        return best

    async def _playback_for_quality(clip: Clip, quality: str) -> Recording:
        """Resolve which recording to actually fetch for the requested quality.

        `sd` -> the independently recorded substream (smaller, faster). `hd` -> the
        mainstream exactly as it came back from the search.

        HD deliberately does NOT run the substream lookup at all: it already holds
        the recording it wants, so a second DVR search would only add latency and
        consume search capacity for a result it would discard.

        SD keeps the v0.1.3 behaviour unchanged, fallback included: when a channel
        has no substream recorded for this window, _substream_for() returns None and
        we quietly serve the mainstream instead. That is a normal outcome with a log
        line, never an error — the user asked for SD as a speed preference, not as a
        precondition, so a channel without one must still play.
        """
        recording = _as_recording(clip)
        if quality == "hd":
            return recording
        if clip.sub_ref:
            # Paired at list time (one sub search per list, not per play), and
            # already validated by registry.put().
            return _as_recording(clip, sub=True)
        playback = await _substream_for(recording)
        if playback is None:
            LOG.info(
                "no substream for channel %s at %s — falling back to the mainstream",
                recording.channel, recording.start.isoformat(),
            )
            return recording
        return playback

    @app.get("/api/stream/{clip_id}")
    async def stream(clip_id: str, request: Request, quality: str = Query(DEFAULT_QUALITY)):
        """Server-side path: fetch, remux to MP4 here, stage, serve with Range.

        This is the COMPATIBILITY path and the automatic fallback for the client-side
        remux in app.js. It is deliberately untouched apart from gaining `quality` —
        it is the safety net, so it must keep working exactly as it did in v0.1.3.
        """
        _check_quality(quality)
        clip = _lookup(clip_id)
        if clip.kind == KIND_REOLINK:
            # Already browser-playable MP4 with moov up front: proxy the bytes with
            # Range passthrough. No remux, no staging. Every open-ended range is
            # clamped to one slice so a paused <video> cannot hold the camera's
            # connection slot (goldfish B2).
            upstream = await _open_reolink(
                clip, _reolink_ref(clip, quality), request.headers.get("range"), clamp=True)
            return _reolink_response(upstream)
        playback = await _playback_for_quality(clip, quality)
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

    @app.get("/api/stream-raw/{clip_id}")
    async def stream_raw(clip_id: str, request: Request, quality: str = Query(DEFAULT_QUALITY)):
        """Raw DVR bytes, unstaged and unmuxed — the client remuxes these itself.

        No ffmpeg runs on the server for this path. The bytes are proxied straight
        through as they arrive, which is what the ORIGINAL pre-Range /api/stream did,
        and is why this is faster: no full-clip staging before the first byte, and no
        server-side transcode at all.

        The response is Hikvision MPEG-PS, NOT playable MP4 — hence video/mpeg, the
        same type /api/download?raw=1 already uses. Nothing but ffmpeg.wasm should
        point a <video> at this.

        Concurrency is not special-cased here: it goes through dvr.stream_download()
        like every other DVR-fetching endpoint, so it competes for the same
        max_concurrent_downloads budget and surfaces the same 503 when saturated.
        """
        _check_quality(quality)
        clip = _lookup(clip_id)
        if clip.kind == KIND_REOLINK:
            raise HTTPException(
                status_code=409,
                detail="This camera already serves playable MP4 — use api/stream.",
            )
        playback = await _playback_for_quality(clip, quality)
        body = await _open_video_stream(playback, raw=True, request=request)
        return StreamingResponse(
            body,
            media_type="video/mpeg",
            headers={"Cache-Control": "no-store"},
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
    async def download(
        clip_id: str, request: Request, raw: int = 0,
        quality: str = Query(DEFAULT_DOWNLOAD_QUALITY),
    ):
        _check_quality(quality)
        clip = _lookup(clip_id)
        name = cameras.get(clip.camera_key, {}).get("name", "camera")
        if clip.kind == KIND_REOLINK:
            # Already MP4, so `raw` has nothing to skip. Not clamped: a download is
            # read continuously; the stall watchdog still frees an abandoned one.
            upstream = await _open_reolink(clip, _reolink_ref(clip, quality), None, clamp=False)
            filename = _safe_filename(name, clip, ".mp4")
            return _reolink_response(
                upstream, {"Content-Disposition": f'attachment; filename="{filename}"'})
        recording = await _playback_for_quality(clip, quality)
        extension = ".mpg" if raw else ".mp4"
        media_type = "video/mpeg" if raw else "video/mp4"
        filename = _safe_filename(name, recording, extension)
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

        Successful JPEGs are kept in an in-memory LRU keyed by
        (camera_key, start, ref_used) so a re-list of the same clip does not
        re-read the device. Errors are not cached.
        """
        clip = _lookup(clip_id)
        # The substream when one was paired at list time: about half the bytes on
        # the DVR (a fifth on the Reolink), so the same 2 MB read reaches further
        # into the clip and frees the device slot sooner. No per-thumbnail search.
        ref_used = clip.sub_ref if clip.sub_ref else clip.main_ref
        cache_key = (
            clip.camera_key,
            clip.start.isoformat().replace("+00:00", "Z"),
            ref_used,
        )

        async def fetch() -> bytes:
            upstream = None
            try:
                if clip.kind == KIND_REOLINK:
                    upstream = await reolink_by_key[clip.camera_key].open(
                        ref_used, f"bytes=0-{THUMB_MAX_BYTES - 1}")
                    source = upstream.body
                else:
                    source = dvr.stream_download(ref_used)
                return await thumbnail_from_stream(source)
            finally:
                if upstream is not None:
                    await upstream.aclose()

        try:
            jpeg = await thumb_cache.get_or_fetch(cache_key, fetch)
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
        """Serve the shell with version-stamped asset URLs.

        Belt and braces with the no-cache header above. If a browser is still
        holding a previous version's app.js and considers it fresh, it will not
        even ask the server — so it never learns about the new policy. Stamping the
        version into the URL sidesteps that entirely: `app.js?v=0.1.6` is a
        different resource from `app.js?v=0.1.4`, so there is nothing to reuse.
        This is what stops a frontend upgrade from silently not taking effect.
        """
        html = (WWW_DIR / "index.html").read_text(encoding="utf-8")
        for asset in ("app.js", "style.css", "vendor/ffmpeg.js"):
            html = html.replace(f'"{asset}"', f'"{asset}?v={VERSION}"')
        return Response(
            content=html,
            media_type="text/html; charset=utf-8",
            headers={"Cache-Control": NO_CACHE},
        )

    # Mounted last so it can never shadow the /api routes above.
    app.mount("/", _CachePolicyStatic(directory=str(WWW_DIR)), name="static")
    return app


def _build_default_app() -> FastAPI | None:
    try:
        return create_app(load_config_from_file())
    except Exception as exc:  # noqa: BLE001 — importing under pytest must not explode
        LOG.debug("default app not built: %s", exc)
        return None


app = _build_default_app()
