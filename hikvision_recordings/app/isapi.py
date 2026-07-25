"""Hikvision ISAPI client — read-only.

Two calls do all the work: ContentMgmt/search finds recordings the DVR already
holds, ContentMgmt/download streams one of them back as raw bytes. This module
never writes to the device and never stores video.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Callable
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as xml_escape

import httpx

from .config import Config

LOG = logging.getLogger(__name__)

SEARCH_PATH = "/ISAPI/ContentMgmt/search"
DOWNLOAD_PATH = "/ISAPI/ContentMgmt/download"
TIME_PATH = "/ISAPI/System/time"

XML_HEADERS = {"Content-Type": "application/xml"}
DVR_TIME_FMT = "%Y-%m-%dT%H:%M:%SZ"     # what search bodies and <timeSpan> use
URI_TIME_FMT = "%Y%m%dT%H%M%SZ"         # what the playbackURI query string uses


class DvrError(Exception):
    """Base for every DVR-side failure. Carries a message safe to show a user."""


class DvrUnreachable(DvrError):
    """Network-level failure: no route, refused, or timed out."""


class DvrAuthError(DvrError):
    """The DVR rejected the configured username/password."""


class DvrBadRequest(DvrError):
    """The DVR rejected our request body."""


class DvrBusy(DvrError):
    """The DVR is at its session limit or otherwise refusing more work."""


@dataclass(frozen=True)
class Recording:
    channel: int
    start: datetime          # tz-aware, UTC
    end: datetime            # tz-aware, UTC
    size_bytes: int
    playback_uri: str        # opaque token from the DVR — NOT a URL we connect to

    @property
    def duration_s(self) -> int:
        return int((self.end - self.start).total_seconds())


def build_search_body(
    channel: int,
    start: str,
    end: str,
    max_results: int,
    position: int = 0,
) -> str:
    """Build a CMSearchDescription. `start`/`end` are DVR wall-clock, DVR_TIME_FMT."""
    # ── DO NOT "SIMPLIFY" THIS TO A STATIC STRING ────────────────────────────────
    # <searchID> MUST be a real UUID (uuid4). Hikvision firmware silently rejects
    # anything else — e.g. "C1" — with:
    #     <statusCode>6</statusCode><statusString>Invalid XML Content</statusString>
    # and gives no indication that searchID is the offending element. Cost hours on
    # 2026-07-24. A fresh UUID per search; it is also the paging handle.
    # ─────────────────────────────────────────────────────────────────────────────
    search_id = str(uuid.uuid4())
    return (
        "<CMSearchDescription>"
        f"<searchID>{search_id}</searchID>"
        f"<trackIDList><trackID>{int(channel)}</trackID></trackIDList>"
        "<timeSpanList><timeSpan>"
        f"<startTime>{xml_escape(start)}</startTime>"
        f"<endTime>{xml_escape(end)}</endTime>"
        "</timeSpan></timeSpanList>"
        f"<maxResults>{int(max_results)}</maxResults>"
        f"<searchResultPosition>{int(position)}</searchResultPosition>"
        "</CMSearchDescription>"
    )


def _local(tag: str) -> str:
    """ElementTree keeps the Hikvision namespace on every tag; strip it."""
    return tag.rsplit("}", 1)[-1]


def _first_text(element: ET.Element, name: str) -> str | None:
    for child in element.iter():
        if _local(child.tag) == name and child.text:
            return child.text.strip()
    return None


def _parse_dvr_time(value: str) -> datetime:
    """Parse a DVR timestamp into a *naive* datetime (DVR wall clock).

    The DVR writes a 'Z' suffix but may mean local time — see design spec 3.5.
    Interpretation is the DvrClock's job, not this parser's.
    """
    cleaned = value.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1]
    # Some firmwares emit a real offset (2026-07-24T13:20:29-05:00); drop it here,
    # the clock offset is derived once from System/time instead.
    if len(cleaned) > 19 and cleaned[19] in "+-":
        cleaned = cleaned[:19]
    return datetime.strptime(cleaned, "%Y-%m-%dT%H:%M:%S")


def _size_from_uri(uri: str) -> int:
    query = parse_qs(urlparse(uri).query)
    try:
        return int(query.get("size", ["0"])[0])
    except (TypeError, ValueError):
        return 0


def parse_search_response(
    xml_text: str,
    channel: int,
    to_utc: Callable[[datetime], datetime],
) -> list[Recording]:
    """Turn a CMSearchResult into Recordings. `to_utc` converts DVR wall clock → UTC."""
    root = ET.fromstring(xml_text)

    if _local(root.tag) == "ResponseStatus":
        code = _first_text(root, "statusCode") or "?"
        text = _first_text(root, "statusString") or "unknown error"
        if code == "6":
            raise DvrBadRequest(
                "The DVR rejected the search request (statusCode 6, badXmlContent). "
                "If <searchID> is not a real uuid4, that is why — see design spec 3.2."
            )
        raise DvrBadRequest(f"The DVR rejected the request: {text} (statusCode {code}).")

    recordings: list[Recording] = []
    for item in root.iter():
        if _local(item.tag) != "searchMatchItem":
            continue
        uri = _first_text(item, "playbackURI")
        start_raw = _first_text(item, "startTime")
        end_raw = _first_text(item, "endTime")
        if not (uri and start_raw and end_raw):
            LOG.warning("skipping searchMatchItem with missing fields")
            continue
        recordings.append(
            Recording(
                channel=channel,
                start=to_utc(_parse_dvr_time(start_raw)),
                end=to_utc(_parse_dvr_time(end_raw)),
                size_bytes=_size_from_uri(uri),
                playback_uri=uri,
            )
        )
    return recordings


# ── ISAPI client — clock, search call, streaming download, concurrency guard ─

DEFAULT_CHUNK = 262144      # 256 KiB — big enough to keep ffmpeg fed, small enough to stream
BUSY_TIMEOUT_S = 10.0       # how long to wait for a free DVR slot before saying "busy"
CLOCK_TTL_S = 300.0         # re-measure the DVR clock at least this often (drift + DST)


@dataclass(frozen=True)
class DvrClock:
    """Translates between UTC and the DVR's wall clock.

    `offset` is defined so that:  dvr_wallclock = utc + offset

    ── HOW THIS DVR ACTUALLY BEHAVES — VERIFIED LIVE 2026-07-25 ──────────────────
    On DVR-THD30B-81-HIK, ContentMgmt/search timestamps are the device's LOCAL wall
    clock wearing a bogus 'Z'. Measured head-to-head over the last 40 real minutes,
    searching for recordings that demonstrably existed:
        offset 0  (treat 'Z' as true UTC) -> ch101=0  ch301=0   MISS
        offset -6h (declared in <localTime>) -> ch101=0  ch301=0   MISS
        offset measured from the device clock -> ch101=1  ch301=4   HIT
    Beware the trap that produced two wrong conclusions before this was settled:
    a WIDE search window (say 6h) numerically spans both frames, so it returns
    matches under either hypothesis and looks like proof. Only a NARROW window
    discriminates.

    ── WHY WE MEASURE THE OFFSET INSTEAD OF READING IT ───────────────────────────
    The offset is NOT taken from the UTC offset declared in <localTime>. That field
    reported -06:00 (base CST) while the device's clock was actually running at
    UTC-5 (CDT) and was ~12 min adrift, so trusting the declared value put every
    query 47:54 off target and returned nothing. We instead measure
        offset = device_wall_clock_naive - our_utc_now
    which absorbs the DST error AND the device's clock drift in one step, and is
    self-correcting: a firmware whose search endpoint really is UTC-honest reports
    a wall clock equal to UTC, measures ~0, and gets no shift applied. Do not
    "simplify" this back to reading the declared offset.
    ──────────────────────────────────────────────────────────────────────────────
    """

    offset: timedelta
    source: str  # "measured" | "configured" | "stale" | "fallback_utc"
    # Wall-clock skew of the device against real UTC, for display/health only.
    # A device on a correct UTC-5 zone with a perfect clock shows ~0 here.
    drift: timedelta = timedelta(0)

    def to_dvr(self, dt_utc: datetime) -> str:
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=timezone.utc)
        return (dt_utc.astimezone(timezone.utc) + self.offset).strftime(DVR_TIME_FMT)

    def to_utc(self, naive_dvr: datetime) -> datetime:
        return (naive_dvr - self.offset).replace(tzinfo=timezone.utc)


def _device_wall_clock(xml_text: str) -> datetime | None:
    """Read the device's own wall-clock reading from <localTime>, as a NAIVE datetime.

    Deliberately discards the declared UTC offset: it reported the base zone (-06:00)
    while the clock ran on DST (-05:00), which is what broke every query. Only the
    wall-clock face value is used; the offset is measured against our UTC instead.
    """
    root = ET.fromstring(xml_text)
    raw = _first_text(root, "localTime")
    if not raw:
        return None
    try:
        aware = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return aware.replace(tzinfo=None)


def _offset_from_device_time(xml_text: str) -> timedelta | None:
    """Derive the DVR wall-clock offset from <localTime>, e.g. 2026-07-24T13:20:29-05:00.

    RETAINED FOR `dvr_time_mode: declared` ONLY. This is what the code originally used
    everywhere, and it is wrong on this hardware whenever DST is in effect — see the
    DvrClock docstring. Do not wire it back into the default path.
    """
    root = ET.fromstring(xml_text)
    raw = _first_text(root, "localTime")
    if not raw:
        return None
    try:
        aware = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if aware.utcoffset() is None:
        return None
    # Round to the nearest 15 minutes — every real timezone lands on one, and this
    # absorbs the second-or-two of clock skew between us and the DVR.
    minutes = round(aware.utcoffset().total_seconds() / 900) * 15
    return timedelta(minutes=minutes)


class IsapiClient:
    """Async ISAPI client. One instance per add-on process."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            auth=httpx.DigestAuth(config.dvr_username, config.dvr_password),
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
            follow_redirects=False,
        )
        self._sem = asyncio.Semaphore(config.max_concurrent_downloads)
        self.clock = DvrClock(offset=timedelta(0), source="fallback_utc")
        self._clock_measured_at = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def ping(self) -> None:
        """Cheap reachability + auth check for /api/health.

        Deliberately NOT a search: a zero-width or degenerate timeSpan has unknown
        firmware behaviour and could report a healthy DVR as unreachable. System/time
        exercises the same network path and the same digest auth, and nothing else.
        """
        try:
            response = await self._client.get(TIME_PATH)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
            raise DvrUnreachable(
                f"Can't reach the DVR at {self._config.dvr_host}."
            ) from exc
        except httpx.HTTPError as exc:
            raise DvrUnreachable(f"DVR request failed: {exc}") from exc
        if response.status_code == 401:
            raise DvrAuthError("The DVR rejected the username or password.")
        if response.status_code >= 400:
            raise DvrUnreachable(f"DVR returned HTTP {response.status_code}.")

    async def probe_clock(self, now_utc: datetime | None = None) -> DvrClock:
        """Measure how this DVR labels time. Never fatal — degrades with a warning.

        `now_utc` is injectable so tests can pin "now" and assert the exact window
        that goes on the wire.
        """
        now_utc = now_utc or datetime.now(timezone.utc)
        mode = self._config.dvr_time_mode

        if mode == "utc":
            # Explicit "the search endpoint is UTC-honest" override. Verified WRONG for
            # DVR-THD30B-81-HIK (returns zero matches) — kept for other firmwares.
            self.clock = DvrClock(timedelta(0), "configured")
            self._clock_measured_at = time.monotonic()
            return self.clock
        if mode == "local":
            local_offset = datetime.now().astimezone().utcoffset() or timedelta(0)
            self.clock = DvrClock(local_offset, "configured")
            self._clock_measured_at = time.monotonic()
            return self.clock
        if mode == "declared":
            try:
                response = await self._client.get(TIME_PATH)
                response.raise_for_status()
                declared = _offset_from_device_time(response.text)
            except (httpx.HTTPError, ET.ParseError):
                declared = None
            self.clock = DvrClock(declared or timedelta(0), "configured")
            self._clock_measured_at = time.monotonic()
            return self.clock

        # mode == "auto": measure the device's clock against ours.
        try:
            response = await self._client.get(TIME_PATH)
            response.raise_for_status()
            device_wall = _device_wall_clock(response.text)
        except (httpx.HTTPError, ET.ParseError) as exc:
            LOG.warning("could not read DVR time (%s)", exc)
            device_wall = None

        if device_wall is None:
            # Keep the last good measurement rather than inventing one. Assuming the
            # host's local zone is what produced the original bug, so we do NOT do that.
            if self.clock.source in ("measured", "stale"):
                self.clock = DvrClock(self.clock.offset, "stale", self.clock.drift)
                LOG.warning("DVR clock unreadable; reusing last measured offset %s",
                            self.clock.offset)
            else:
                self.clock = DvrClock(timedelta(0), "fallback_utc")
                LOG.error("DVR clock unreadable and never measured; searches may miss")
            return self.clock

        naive_now = now_utc.astimezone(timezone.utc).replace(tzinfo=None)
        # Whole seconds: sub-second precision here is meaningless (it is just when the
        # HTTP round-trip landed) and would otherwise leak microseconds into every
        # reported clip timestamp.
        offset = timedelta(seconds=round((device_wall - naive_now).total_seconds()))
        # Drift = how far the device's clock sits from the nearest whole-hour zone,
        # i.e. the part of the offset that is a wrong clock rather than a timezone.
        drift = offset - timedelta(hours=round(offset.total_seconds() / 3600))
        self.clock = DvrClock(offset, "measured", drift)
        self._clock_measured_at = time.monotonic()
        LOG.info(
            "DVR clock measured: offset %s (device reads %s, our UTC %s, drift %s)",
            offset, device_wall, naive_now, drift,
        )
        return self.clock

    async def _fresh_clock(self) -> DvrClock:
        """Re-measure if the cached offset is stale.

        The device's clock drifts (~12 min observed on this unit) and DST transitions
        move the offset by an hour, so a once-at-startup measurement silently rots.
        """
        if (
            self.clock.source == "configured"
            or time.monotonic() - self._clock_measured_at < CLOCK_TTL_S
        ):
            return self.clock
        return await self.probe_clock()

    async def search(
        self,
        channel: int,
        start_utc: datetime,
        end_utc: datetime,
        max_results: int | None = None,
    ) -> list[Recording]:
        clock = await self._fresh_clock()
        body = build_search_body(
            channel,
            clock.to_dvr(start_utc),
            clock.to_dvr(end_utc),
            max_results or self._config.max_results,
        )
        try:
            response = await self._client.post(
                SEARCH_PATH, content=body.encode("utf-8"), headers=XML_HEADERS
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
            raise DvrUnreachable(
                f"Can't reach the DVR at {self._config.dvr_host}. "
                "Is it powered on and on the network?"
            ) from exc
        except httpx.HTTPError as exc:
            raise DvrUnreachable(f"DVR request failed: {exc}") from exc

        if response.status_code == 401:
            raise DvrAuthError(
                "The DVR rejected the username or password — "
                "check the add-on configuration."
            )
        if response.status_code >= 500:
            raise DvrBusy("The DVR is busy. Try again in a moment.")
        if response.status_code >= 400:
            raise DvrBadRequest(f"The DVR rejected the search (HTTP {response.status_code}).")

        return parse_search_response(response.text, channel, clock.to_utc)

    async def stream_download(
        self, playback_uri: str, chunk_size: int = DEFAULT_CHUNK
    ) -> AsyncIterator[bytes]:
        """Yield the recording's raw bytes (Hikvision MPEG-PS). Nothing is buffered to disk."""
        body = (
            "<downloadRequest>"
            f"<playbackURI>{xml_escape(playback_uri)}</playbackURI>"
            "</downloadRequest>"
        )
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=BUSY_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            raise DvrBusy(
                "DVR is busy serving another stream. Try again in a moment."
            ) from exc
        try:
            async with self._client.stream(
                "POST", DOWNLOAD_PATH, content=body.encode("utf-8"), headers=XML_HEADERS
            ) as response:
                if response.status_code == 401:
                    raise DvrAuthError(
                        "The DVR rejected the username or password — "
                        "check the add-on configuration."
                    )
                if response.status_code >= 500:
                    raise DvrBusy("DVR is busy serving another stream. Try again in a moment.")
                if response.status_code >= 400:
                    raise DvrBadRequest(
                        f"The DVR refused the download (HTTP {response.status_code})."
                    )
                async for chunk in response.aiter_bytes(chunk_size):
                    yield chunk
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
            raise DvrUnreachable(
                f"Lost the connection to the DVR at {self._config.dvr_host}."
            ) from exc
        except httpx.HTTPError as exc:
            raise DvrUnreachable(f"DVR request failed: {exc}") from exc
        finally:
            self._sem.release()
