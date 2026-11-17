"""Reolink camera client — read-only, for cameras that record to their own SD card.

Verified live against a Reolink E1 Pro (E Series E330, firmware v3.1.0.4417) on
2026-11-14 with scripts/verify_reolink_contract.py; see designs/2026-11-14-reolink-and-ui.md
§2 and §9.1. The facts this module is built on:

  * POST /cgi-bin/api.cgi?cmd=X&token=T with a JSON array body. `Login` returns a
    token with a 3600 s lease. Cameras allow only a few sessions, and a token that
    is never logged out holds one for up to an hour — so one token is reused, and
    it is logged out on shutdown.
  * `Search` returns SD-card files with camera-LOCAL wall-clock Start/EndTime.
    A window that crosses midnight returns ZERO files, so every search is split
    per camera-local day. Two Search commands batched in one request also fail
    (code 1), so it is one request per (stream, day).
  * Files are MP4 (H.264 + AAC) with `moov` at the front, so the browser plays them
    as-is: no remux, no staging. `cmd=Download` honours single byte ranges (206,
    416 past the end); a multi-range request comes back as a malformed 206, so only
    single ranges are ever forwarded.
  * A bad token is a 401 text/html, a missing file a 404 HTML page — errors never
    arrive as a 200, so the status code alone tells them apart from video.

The session token never leaves this module: it is only ever put on the wire via
httpx `params=`, and no exception text containing a URL is ever surfaced.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Awaitable, Callable

import httpx

from .clips import KIND_REOLINK, SUB_WINDOW_PAD, Clip, best_overlap
from .config import ReolinkCamera
from .reolink_flags import events_from_filename
from .isapi import (
    BUSY_TIMEOUT_S,
    CLOCK_TTL_S,
    ClipGone,
    DvrAuthError,
    DvrBadRequest,
    DvrBusy,
    DvrClock,
    DvrUnreachable,
    RangeNotSatisfiable,
)

LOG = logging.getLogger(__name__)

API_PATH = "/cgi-bin/api.cgi"
CHUNK = 262144
# Every open-ended Range is answered with at most this many bytes. A paused or
# abandoned <video> then holds a device slot for one short request, not for the
# rest of the clip (goldfish B2). Browsers simply ask for the next slice.
SLICE_BYTES = 8 * 1024 * 1024
# If the browser stops reading for this long, give the slot back.
IDLE_READ_TIMEOUT_S = 15.0
# After the camera rejects the credentials, do not try again for this long —
# Reolink locks an account after repeated failures.
AUTH_BACKOFF_S = 300.0
LEASE_MARGIN_S = 300.0
LOGOUT_TIMEOUT_S = 3.0

_LOGIN_AGAIN = "please login first"
_BAD_CREDENTIALS = ("invalid user", "login failed", "password wrong", "login has been locked")

# Mp4Record/2026-11-14/RecM0A_20261114_183508_183650_0_9B28C080000000_41D8273.mp4
# Mp4Record/2026-11-01/RecM0A_DST20261101_003727_003751_0_9B288..._....mp4  (DST)
_REF_RE = re.compile(
    r"Mp4Record/\d{4}-\d{2}-\d{2}/Rec[MS][0-9A-Za-z]{2}_(?:DST)?\d{8}_\d{6}_\d{6}_[0-9A-Za-z_]+\.mp4"
)
_DST_RE = re.compile(r"/Rec[MS][0-9A-Za-z]{2}_DST\d{8}_")
_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


class InvalidReolinkRef(ValueError):
    """Not a Reolink SD-card file name of the verified shape. Never fetched."""


def validate_reolink_ref(ref: str) -> None:
    # fullmatch over a fixed charset: no '..', no '&', '#', '%', whitespace or CR/LF.
    if not isinstance(ref, str) or not _REF_RE.fullmatch(ref):
        raise InvalidReolinkRef("not a Reolink recording file name — refusing to use it")


def sanitize_range(header: str | None, clamp: bool) -> str | None:
    """Forward only a single, well-formed byte range; optionally cap an OPEN one.

    Anything else (multi-range, junk) is treated as no Range at all, because this
    firmware answers a multi-range request with a 206 that has no Content-Range.

    Only open-ended ranges (`bytes=N-`, what Chrome sends) are clamped. A closed
    range is passed through exactly: Safari/AVFoundation asks for explicit ranges
    and is strict about receiving the range it asked for. The stall watchdog, not
    the clamp, is what keeps a paused player from holding a slot.
    """
    if not header:
        return None
    match = _RANGE_RE.fullmatch(header.strip())
    if not match:
        return None
    first, last = match.groups()
    if not first and not last:
        return None
    if not first:
        return f"bytes=-{int(last)}"            # suffix range: already bounded
    start = int(first)
    if last:
        end = int(last)
        if end < start:
            return None
        return f"bytes={start}-{end}"
    return f"bytes={start}-{start + SLICE_BYTES - 1}" if clamp else f"bytes={start}-"


@dataclass
class Upstream:
    """An open byte stream from a device. `aclose()` MUST be called (idempotent);
    it is what releases the device's connection slot, even if `body` was never
    iterated — an async generator that never started does not run its finally."""

    status: int
    headers: dict[str, str]
    body: AsyncIterator[bytes]
    _closer: Callable[[], Awaitable[None]] = field(repr=False, default=None)

    async def aclose(self) -> None:
        await self.body.aclose()
        if self._closer is not None:
            await self._closer()


def _rtime(dt: datetime) -> dict:
    return {"year": dt.year, "mon": dt.month, "day": dt.day,
            "hour": dt.hour, "min": dt.minute, "sec": dt.second}


def _from_rtime(t: dict) -> datetime:
    return datetime(int(t["year"]), int(t["mon"]), int(t["day"]),
                    int(t["hour"]), int(t["min"]), int(t["sec"]))


def day_spans(lo: datetime, hi: datetime) -> list[tuple[datetime, datetime]]:
    """Split [lo, hi] (camera-local, naive) into per-calendar-day spans.

    Each span is clipped to [D 00:00:00, D 23:59:59] — a Search that crosses
    midnight returns nothing on this firmware.
    """
    spans = []
    day = lo.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= hi:
        start = max(lo, day)
        end = min(hi, day.replace(hour=23, minute=59, second=59))
        if start <= end:
            spans.append((start, end))
        day += timedelta(days=1)
    return spans


@dataclass(frozen=True)
class _CameraTime:
    """How this camera's wall clock relates to UTC right now."""

    offset: timedelta          # camera_wallclock = utc + offset   (measured)
    now_dst: bool              # is DST in effect on the camera right now
    dst_hours: int             # DST shift in hours (0 if the camera has DST off)

    def to_utc(self, local: datetime, clip_dst: bool | None) -> datetime:
        # The measured offset is right for clips recorded under the CURRENT DST
        # state; a clip recorded under the other state is an hour away from it.
        # The camera marks DST recordings in the file name (verified: _DST2026…).
        shift = timedelta(0)
        if clip_dst is not None and self.dst_hours:
            shift = timedelta(hours=self.dst_hours * (int(clip_dst) - int(self.now_dst)))
        return (local - self.offset - shift).replace(tzinfo=timezone.utc)

    def to_local(self, utc: datetime) -> datetime:
        return utc.astimezone(timezone.utc).replace(tzinfo=None) + self.offset


class ReolinkClient:
    """Async client for one Reolink camera. One instance per configured camera."""

    kind = KIND_REOLINK

    def __init__(self, camera: ReolinkCamera, key: str, max_concurrent: int) -> None:
        self.camera = camera
        self.key = key
        self._http = httpx.AsyncClient(
            base_url=camera.base_url,
            verify=camera.verify_ssl,
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
            follow_redirects=False,
        )
        self._sem = asyncio.Semaphore(max_concurrent)
        self._search_sem = asyncio.Semaphore(1)
        self._login_lock = asyncio.Lock()
        self._token: str | None = None
        self._token_valid_until = 0.0
        self._auth_blocked_until = 0.0
        self._time: _CameraTime | None = None
        self._time_measured_at = 0.0
        self.clock = DvrClock(offset=timedelta(0), source="fallback_utc")

    # ── session ────────────────────────────────────────────────────────────
    def _unreachable(self) -> DvrUnreachable:
        return DvrUnreachable(f"Can't reach the camera {self.camera.name}.")

    def _auth_error(self) -> DvrAuthError:
        return DvrAuthError(f"The camera {self.camera.name} rejected the username or password.")

    async def _ensure_token(self) -> str:
        if self._token and time.monotonic() < self._token_valid_until:
            return self._token
        async with self._login_lock:
            # Another request may have logged in while this one waited.
            if self._token and time.monotonic() < self._token_valid_until:
                return self._token
            if time.monotonic() < self._auth_blocked_until:
                raise self._auth_error()
            body = [{"cmd": "Login", "action": 0, "param": {"User": {
                "userName": self.camera.username, "password": self.camera.password}}}]
            try:
                response = await self._http.post(API_PATH, params={"cmd": "Login"}, json=body)
            except httpx.HTTPError as exc:
                raise self._unreachable() from exc
            if response.status_code >= 400:
                raise DvrUnreachable(
                    f"The camera {self.camera.name} returned HTTP {response.status_code}."
                )
            try:
                data = response.json()[0]
            except (ValueError, IndexError, KeyError) as exc:
                raise DvrBadRequest(f"Unexpected login reply from {self.camera.name}.") from exc
            if data.get("code") != 0:
                detail = str((data.get("error") or {}).get("detail", "")).lower()
                if any(marker in detail for marker in _BAD_CREDENTIALS):
                    self._auth_blocked_until = time.monotonic() + AUTH_BACKOFF_S
                    raise self._auth_error()
                # e.g. all sessions in use — transient, no backoff.
                raise DvrBusy(f"The camera {self.camera.name} refused a login. Try again shortly.")
            token = data["value"]["Token"]
            self._token = str(token["name"])
            lease = float(token.get("leaseTime", 3600))
            self._token_valid_until = time.monotonic() + max(lease - LEASE_MARGIN_S, 30.0)
            return self._token

    def _drop_token(self, failed: str) -> None:
        # Only forget the token that was rejected. Two requests can fail on the
        # same stale token at once; if the first already logged in again, the
        # second must not throw that fresh token away (and leak a session).
        if self._token == failed:
            self._token = None

    async def _api(self, cmd: str, param: dict, retry: bool = True) -> dict:
        token = await self._ensure_token()
        body = [{"cmd": cmd, "action": 0, "param": param}]
        try:
            response = await self._http.post(
                API_PATH, params={"cmd": cmd, "token": token}, json=body
            )
        except httpx.HTTPError as exc:
            raise self._unreachable() from exc
        if response.status_code == 401 and retry:
            self._drop_token(token)
            return await self._api(cmd, param, retry=False)
        if response.status_code >= 400:
            raise DvrUnreachable(
                f"The camera {self.camera.name} returned HTTP {response.status_code}."
            )
        try:
            data = response.json()[0]
        except (ValueError, IndexError, KeyError) as exc:
            raise DvrBadRequest(f"Unexpected reply from {self.camera.name}.") from exc
        if data.get("code") != 0:
            detail = str((data.get("error") or {}).get("detail", "")).lower()
            if _LOGIN_AGAIN in detail and retry:
                self._drop_token(token)
                return await self._api(cmd, param, retry=False)
            raise DvrBadRequest(f"The camera {self.camera.name} rejected {cmd}.")
        return data.get("value") or {}

    async def aclose(self) -> None:
        token, self._token = self._token, None
        if token:
            try:
                await asyncio.wait_for(
                    self._http.post(
                        API_PATH,
                        params={"cmd": "Logout", "token": token},
                        json=[{"cmd": "Logout", "action": 0, "param": {}}],
                    ),
                    timeout=LOGOUT_TIMEOUT_S,
                )
            except Exception as exc:  # noqa: BLE001 — shutdown must not fail
                LOG.warning("logout from %s failed (%s)", self.camera.name, type(exc).__name__)
        await self._http.aclose()

    # ── clock ──────────────────────────────────────────────────────────────
    async def probe_clock(self, now_utc: datetime | None = None) -> DvrClock:
        value = await self._api("GetTime", {})
        now_utc = now_utc or datetime.now(timezone.utc)
        t = value.get("Time") or {}
        dst = value.get("Dst") or {}
        device_wall = _from_rtime(t)
        naive_now = now_utc.astimezone(timezone.utc).replace(tzinfo=None)
        offset = timedelta(seconds=round((device_wall - naive_now).total_seconds()))
        dst_hours = int(dst.get("offset", 1) or 0) if int(dst.get("enable", 0) or 0) else 0
        self._time = _CameraTime(offset, bool(int(t.get("isDst", 0) or 0)), dst_hours)
        self._time_measured_at = time.monotonic()
        drift = offset - timedelta(hours=round(offset.total_seconds() / 3600))
        self.clock = DvrClock(offset, "measured", drift)
        return self.clock

    async def _fresh_time(self) -> _CameraTime:
        if self._time is None or time.monotonic() - self._time_measured_at >= CLOCK_TTL_S:
            await self.probe_clock()
        return self._time

    async def ping(self) -> None:
        await self.probe_clock()

    # ── search ─────────────────────────────────────────────────────────────
    async def _search_files(self, stream: str, lo: datetime, hi: datetime) -> list[dict]:
        async with self._search_sem:
            value = await self._api("Search", {"Search": {
                "channel": self.camera.channel, "onlyStatus": 0, "streamType": stream,
                "StartTime": _rtime(lo), "EndTime": _rtime(hi)}})
        return (value.get("SearchResult") or {}).get("File") or []

    def _to_entries(self, files: dict[str, dict], cam_time: _CameraTime):
        entries = []
        for name, f in files.items():
            try:
                start_local = _from_rtime(f["StartTime"])
                end_local = _from_rtime(f["EndTime"])
                size = int(f.get("size") or 0)
            except (KeyError, TypeError, ValueError):
                LOG.warning("skipping a Reolink file entry with missing fields")
                continue
            if end_local <= start_local:
                continue    # zero-length stub files exist (8-byte mdat) — verified
            is_dst = bool(_DST_RE.search(name))
            entries.append((name, cam_time.to_utc(start_local, is_dst),
                            cam_time.to_utc(end_local, is_dst), size))
        return entries

    async def search(
        self, start_utc: datetime, end_utc: datetime, limit: int
    ) -> tuple[list[Clip], bool]:
        """Clips overlapping [start_utc, end_utc], oldest first, plus `truncated`."""
        cam_time = await self._fresh_time()
        # Pad by the DST shift (clips under the other DST state sit an hour away
        # from the measured offset) and by the sub-pairing slack; results are
        # filtered back to the exact UTC window below.
        pad = timedelta(hours=cam_time.dst_hours) + SUB_WINDOW_PAD
        lo = cam_time.to_local(start_utc) - pad
        hi = cam_time.to_local(end_utc) + pad

        mains: dict[str, dict] = {}
        subs: dict[str, dict] = {}
        for span_lo, span_hi in day_spans(lo, hi):
            for f in await self._search_files("main", span_lo, span_hi):
                mains[f.get("name", "")] = f      # dedupe across day spans by name
            for f in await self._search_files("sub", span_lo, span_hi):
                subs[f.get("name", "")] = f

        sub_entries = self._to_entries(subs, cam_time)
        clips = []
        for name, start, end, size in self._to_entries(mains, cam_time):
            if end < start_utc or start > end_utc:
                continue
            sub = best_overlap(start, end, sub_entries, key=lambda e: (e[1], e[2]))
            clips.append(Clip(
                camera_key=self.key, kind=KIND_REOLINK, channel=self.camera.channel,
                start=start, end=end, size_bytes=size, main_ref=name,
                sub_ref=sub[0] if sub else None, sub_size_bytes=sub[3] if sub else None,
                events=events_from_filename(name),
            ))
        clips.sort(key=lambda c: c.start)
        truncated = len(clips) > limit
        # Keep the newest: the list is shown newest first.
        return (clips[-limit:] if truncated else clips), truncated

    # ── bytes ──────────────────────────────────────────────────────────────
    async def _send_download(self, ref: str, rng: str | None, retry: bool = True) -> httpx.Response:
        token = await self._ensure_token()
        headers = {"Accept-Encoding": "identity"}
        if rng:
            headers["Range"] = rng
        request = self._http.build_request(
            "GET", API_PATH,
            params={"cmd": "Download", "source": ref,
                    "output": ref.rsplit("/", 1)[-1], "token": token},
            headers=headers,
        )
        try:
            response = await self._http.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise self._unreachable() from exc

        status = response.status_code
        if status == 401 and retry:
            await response.aclose()
            self._drop_token(token)
            return await self._send_download(ref, rng, retry=False)
        if status in (200, 206):
            content_type = response.headers.get("content-type", "")
            if content_type.startswith("text/"):
                await response.aclose()
                raise DvrBadRequest(f"The camera {self.camera.name} returned a page, not video.")
            return response
        await response.aclose()
        if status == 401:
            raise self._auth_error()
        if status == 404:
            raise ClipGone("That recording is no longer on the camera — search again.")
        if status == 416:
            raise RangeNotSatisfiable("Requested range is outside the clip.",
                                      response.headers.get("content-range"))
        raise DvrUnreachable(f"The camera {self.camera.name} returned HTTP {status}.")

    async def open(self, ref: str, range_header: str | None = None,
                   clamp: bool = False, busy_timeout: float | None = None) -> Upstream:
        """Open a recording's bytes, forwarding (a sanitised) Range.

        Holds one of this camera's connection slots until Upstream.aclose().
        `clamp=True` is for <video> playback: open-ended ranges are capped to one
        slice, and a reader that stops for IDLE_READ_TIMEOUT_S loses the slot.
        Downloads pass False — a browser's "save as?" prompt may legitimately
        leave the body unread for a while, and a cut-off download is a broken file.
        External link fetches pass `busy_timeout=30` (UI keeps the 10 s default).
        """
        validate_reolink_ref(ref)
        rng = sanitize_range(range_header, clamp)
        wait_s = BUSY_TIMEOUT_S if busy_timeout is None else busy_timeout
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=wait_s)
        except asyncio.TimeoutError as exc:
            raise DvrBusy(f"The camera {self.camera.name} is busy. Try again in a moment.") from exc

        try:
            response = await self._send_download(ref, rng)
        except BaseException:
            self._sem.release()
            raise

        released = False

        async def close() -> None:
            nonlocal released
            if released:
                return
            released = True
            try:
                await response.aclose()
            finally:
                self._sem.release()

        async def body() -> AsyncIterator[bytes]:
            # Two different stalls, two guards:
            #  * the CAMERA stops sending  -> wait_for() on each read;
            #  * the BROWSER stops reading (paused <video>) -> this generator sits
            #    suspended at `yield` and never regains control, so a timer armed
            #    around every yield closes the upstream and frees the slot from
            #    outside. The browser re-requests a range when it needs more.
            loop = asyncio.get_running_loop()
            watchdog: asyncio.TimerHandle | None = None

            def consumer_stalled() -> None:
                LOG.info("client stopped reading from %s; releasing the slot", self.camera.name)
                loop.create_task(close())

            iterator = response.aiter_raw(CHUNK).__aiter__()
            try:
                while not released:
                    try:
                        chunk = await asyncio.wait_for(iterator.__anext__(), IDLE_READ_TIMEOUT_S)
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        LOG.info("camera %s stalled; releasing the slot", self.camera.name)
                        break
                    except Exception as exc:  # noqa: BLE001 — incl. closed by the watchdog
                        if not released:
                            LOG.warning("camera %s failed mid-stream (%s)",
                                        self.camera.name, type(exc).__name__)
                        break
                    if clamp:
                        watchdog = loop.call_later(IDLE_READ_TIMEOUT_S, consumer_stalled)
                    try:
                        yield chunk
                    finally:
                        if watchdog is not None:
                            watchdog.cancel()
                            watchdog = None
            finally:
                await close()

        headers = {k: response.headers[k] for k in ("content-length", "content-range")
                   if k in response.headers}
        return Upstream(status=response.status_code, headers=headers, body=body(), _closer=close)
