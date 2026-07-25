"""Hikvision ISAPI client — read-only.

Two calls do all the work: ContentMgmt/search finds recordings the DVR already
holds, ContentMgmt/download streams one of them back as raw bytes. This module
never writes to the device and never stores video.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as xml_escape

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
