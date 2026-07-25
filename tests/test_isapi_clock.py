"""Regression tests for the 2026-07-25 live-verified clock bug.

THE BUG: the query offset was taken from the UTC offset *declared* in
`/ISAPI/System/time` (`-06:00`, the base CST zone) while the device's clock was
actually running on DST (UTC-5) and was ~12 min adrift. Every search window went
out ~48 minutes off target and matched nothing.

THE TRAP: a wide search window numerically spans both the UTC and local frames, so
it returns matches under either hypothesis and looks like proof. These tests pin
the exact `<startTime>` string that goes on the wire, which cannot be fooled that
way.

Measured live against DVR-THD30B-81-HIK over the last 40 real minutes, searching
for recordings that demonstrably existed:
    offset 0   (treat 'Z' as true UTC)     -> ch101=0  ch301=0   MISS
    offset -6h (declared in <localTime>)   -> ch101=0  ch301=0   MISS
    offset measured from the device clock  -> ch101=1  ch301=4   HIT
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from hikvision_recordings.app.config import load_config
from hikvision_recordings.app.isapi import IsapiClient

# The device reads 11:30:34 on its own face and *claims* to be at -06:00 …
DEVICE_TIME_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Time xmlns="http://www.hikvision.com/ver20/XMLSchema">
<timeMode>manual</timeMode>
<localTime>2026-07-25T11:30:34-06:00</localTime>
<timeZone>CST+6:00:00DST01:00:00,M3.2.0/02:00:00,M11.1.0/02:00:00</timeZone>
</Time>"""

# … while real UTC is 16:42:40Z. So the *measured* offset is -5:12:06, not the
# declared -6:00. The 47:54 gap is the DST error plus the device's clock drift.
NOW_UTC = datetime(2026, 7, 25, 16, 42, 40, tzinfo=timezone.utc)
MEASURED_OFFSET = timedelta(hours=-5, minutes=-12, seconds=-6)
DECLARED_OFFSET = timedelta(hours=-6)

SEARCH_OK = """<?xml version="1.0" encoding="UTF-8"?>
<CMSearchResult xmlns="http://www.hikvision.com/ver20/XMLSchema">
<searchID>0f4e6d1a-2b3c-4d5e-8f90-123456789abc</searchID>
<responseStatus>true</responseStatus><responseStatusStrg>OK</responseStatusStrg>
<numOfMatches>1</numOfMatches>
<matchList><searchMatchItem>
<trackID>301</trackID>
<timeSpan><startTime>2026-07-25T11:28:55Z</startTime><endTime>2026-07-25T11:29:55Z</endTime></timeSpan>
<mediaSegmentDescriptor>
<playbackURI>rtsp://10.10.11.56/Streaming/tracks/301/?starttime=20260725T112855Z&amp;endtime=20260725T112955Z&amp;name=x&amp;size=63294704</playbackURI>
</mediaSegmentDescriptor>
</searchMatchItem></matchList>
</CMSearchResult>"""

OPTIONS = {
    "dvr_host": "10.10.11.56",
    "dvr_username": "Jimmy",
    "dvr_password": "s3cret",
    "channels": [{"id": 301, "name": "left alley"}],
}


def make_client(**overrides) -> IsapiClient:
    return IsapiClient(load_config({**OPTIONS, **overrides}))


def _mock_time_and_search():
    respx.get("http://10.10.11.56:80/ISAPI/System/time").mock(
        return_value=httpx.Response(200, text=DEVICE_TIME_XML)
    )
    return respx.post("http://10.10.11.56:80/ISAPI/ContentMgmt/search").mock(
        return_value=httpx.Response(200, text=SEARCH_OK)
    )


def _window_on_the_wire(route) -> tuple[str, str]:
    body = route.calls[0].request.content.decode()
    start = re.search(r"<startTime>([^<]+)</startTime>", body).group(1)
    end = re.search(r"<endTime>([^<]+)</endTime>", body).group(1)
    return start, end


@respx.mock
async def test_auto_measures_offset_against_the_device_clock_not_the_declared_one():
    """The whole bug in one assertion.

    Fails on the pre-fix code, which used the declared -06:00 and would put
    11:42:40 / 12:42:40 on the wire instead of 12:30:34 / 13:30:34.
    """
    route = _mock_time_and_search()
    client = make_client(dvr_time_mode="auto")
    clock = await client.probe_clock(now_utc=NOW_UTC)

    assert clock.source == "measured"
    assert clock.offset == MEASURED_OFFSET, (
        "offset must be MEASURED against the device's clock face, not read from the "
        "declared UTC offset — the declared value ignores DST and broke every query"
    )
    assert clock.offset != DECLARED_OFFSET

    await client.search(
        301,
        datetime(2026, 7, 25, 17, 42, 40, tzinfo=timezone.utc),
        datetime(2026, 7, 25, 18, 42, 40, tzinfo=timezone.utc),
    )
    start, end = _window_on_the_wire(route)
    # 17:42:40Z shifted by the measured -5:12:06 -> 12:30:34
    assert start == "2026-07-25T12:30:34Z"
    assert end == "2026-07-25T13:30:34Z"
    # The two known-wrong answers, named explicitly so a regression is unmistakable.
    assert start != "2026-07-25T17:42:40Z", "un-shifted UTC returned zero matches live"
    assert start != "2026-07-25T11:42:40Z", "declared -6h returned zero matches live"
    await client.aclose()


@respx.mock
async def test_results_are_translated_back_with_the_same_measured_offset():
    """A clip labelled 11:28:55 must report as the real UTC instant it happened."""
    _mock_time_and_search()
    client = make_client(dvr_time_mode="auto")
    await client.probe_clock(now_utc=NOW_UTC)
    recs = await client.search(
        301,
        datetime(2026, 7, 25, 16, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 25, 17, 0, tzinfo=timezone.utc),
    )
    assert len(recs) == 1
    # 11:28:55 device-face + 5:12:06 -> 16:41:01Z, i.e. ~2 min before "now". A clip
    # that just happened must not be reported as 5 hours old.
    assert recs[0].start == datetime(2026, 7, 25, 16, 41, 1, tzinfo=timezone.utc)
    assert NOW_UTC - recs[0].start < timedelta(minutes=10)
    await client.aclose()


@respx.mock
async def test_utc_mode_applies_no_shift():
    """Explicit override for firmwares whose search endpoint really is UTC-honest."""
    route = _mock_time_and_search()
    client = make_client(dvr_time_mode="utc")
    clock = await client.probe_clock(now_utc=NOW_UTC)
    assert clock.offset == timedelta(0)
    await client.search(
        301,
        datetime(2026, 7, 25, 17, 42, 40, tzinfo=timezone.utc),
        datetime(2026, 7, 25, 18, 42, 40, tzinfo=timezone.utc),
    )
    start, _ = _window_on_the_wire(route)
    assert start == "2026-07-25T17:42:40Z"
    await client.aclose()


@respx.mock
async def test_declared_mode_still_available_as_an_escape_hatch():
    route = _mock_time_and_search()
    client = make_client(dvr_time_mode="declared")
    clock = await client.probe_clock(now_utc=NOW_UTC)
    assert clock.offset == DECLARED_OFFSET
    await client.search(
        301,
        datetime(2026, 7, 25, 17, 42, 40, tzinfo=timezone.utc),
        datetime(2026, 7, 25, 18, 42, 40, tzinfo=timezone.utc),
    )
    start, _ = _window_on_the_wire(route)
    assert start == "2026-07-25T11:42:40Z"
    await client.aclose()


@respx.mock
async def test_a_utc_honest_firmware_self_detects_to_no_shift():
    """`auto` is safe for other hardware: a device whose face equals UTC measures ~0."""
    respx.get("http://10.10.11.56:80/ISAPI/System/time").mock(
        return_value=httpx.Response(
            200,
            text=DEVICE_TIME_XML.replace(
                "<localTime>2026-07-25T11:30:34-06:00</localTime>",
                "<localTime>2026-07-25T16:42:40+00:00</localTime>",
            ),
        )
    )
    client = make_client(dvr_time_mode="auto")
    clock = await client.probe_clock(now_utc=NOW_UTC)
    assert clock.offset == timedelta(0)
    assert clock.source == "measured"
    await client.aclose()


@respx.mock
async def test_unreadable_clock_reuses_the_last_measurement_instead_of_guessing():
    """Assuming the HOST's zone is what caused the original bug — never do that again."""
    time_route = respx.get("http://10.10.11.56:80/ISAPI/System/time")
    time_route.mock(return_value=httpx.Response(200, text=DEVICE_TIME_XML))
    client = make_client(dvr_time_mode="auto")
    await client.probe_clock(now_utc=NOW_UTC)

    time_route.mock(side_effect=httpx.ConnectError("gone"))
    clock = await client.probe_clock(now_utc=NOW_UTC)
    assert clock.offset == MEASURED_OFFSET
    assert clock.source == "stale"
    await client.aclose()
