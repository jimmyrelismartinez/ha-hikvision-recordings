from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from hikvision_recordings.app.config import load_config
from hikvision_recordings.app.isapi import (
    DvrAuthError,
    DvrBusy,
    DvrUnreachable,
    IsapiClient,
)

OPTIONS = {
    "dvr_host": "10.10.11.56",
    "dvr_username": "Jimmy",
    "dvr_password": "s3cret",
    "channels": [{"id": 101, "name": "DriveWay1"}],
    "max_concurrent_downloads": 1,
}


def make_client(**overrides) -> IsapiClient:
    return IsapiClient(load_config({**OPTIONS, **overrides}))


@respx.mock
async def test_probe_clock_uses_device_time(device_time):
    respx.get("http://10.10.11.56:80/ISAPI/System/time").mock(
        return_value=httpx.Response(200, text=device_time)
    )
    client = make_client()
    clock = await client.probe_clock()
    assert clock.source == "device"
    # localTime is -05:00, so DVR wall clock runs 5h behind UTC
    assert clock.offset == timedelta(hours=-5)
    assert clock.to_dvr(datetime(2026, 7, 24, 18, 20, 29, tzinfo=timezone.utc)) == "2026-07-24T13:20:29Z"
    assert clock.to_utc(datetime(2026, 7, 24, 13, 20, 29)) == datetime(
        2026, 7, 24, 18, 20, 29, tzinfo=timezone.utc
    )
    await client.aclose()


@respx.mock
async def test_time_mode_utc_forces_zero_offset(device_time):
    respx.get("http://10.10.11.56:80/ISAPI/System/time").mock(
        return_value=httpx.Response(200, text=device_time)
    )
    client = make_client(dvr_time_mode="utc")
    clock = await client.probe_clock()
    assert clock.offset == timedelta(0)
    assert clock.source == "configured"
    await client.aclose()


@respx.mock
async def test_ping_succeeds_on_200(device_time):
    respx.get("http://10.10.11.56:80/ISAPI/System/time").mock(
        return_value=httpx.Response(200, text=device_time)
    )
    client = make_client()
    await client.ping()  # must not raise
    await client.aclose()


@respx.mock
async def test_ping_maps_401_to_auth_error():
    respx.get("http://10.10.11.56:80/ISAPI/System/time").mock(
        return_value=httpx.Response(401, text="unauthorized")
    )
    client = make_client()
    with pytest.raises(DvrAuthError):
        await client.ping()
    await client.aclose()


@respx.mock
async def test_ping_maps_connect_error_to_unreachable():
    respx.get("http://10.10.11.56:80/ISAPI/System/time").mock(
        side_effect=httpx.ConnectError("no route to host")
    )
    client = make_client()
    with pytest.raises(DvrUnreachable):
        await client.ping()
    await client.aclose()


@respx.mock
async def test_search_posts_body_and_parses(search_ok):
    route = respx.post("http://10.10.11.56:80/ISAPI/ContentMgmt/search").mock(
        return_value=httpx.Response(200, text=search_ok)
    )
    client = make_client(dvr_time_mode="utc")
    await client.probe_clock()
    recs = await client.search(
        101,
        datetime(2026, 7, 24, 13, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 24, 14, 0, tzinfo=timezone.utc),
    )
    assert len(recs) == 2
    sent = route.calls[0].request.content.decode()
    assert "<trackID>101</trackID>" in sent
    assert "<startTime>2026-07-24T13:00:00Z</startTime>" in sent
    await client.aclose()


@respx.mock
async def test_401_becomes_auth_error(search_ok):
    respx.post("http://10.10.11.56:80/ISAPI/ContentMgmt/search").mock(
        return_value=httpx.Response(401, text="unauthorized")
    )
    client = make_client(dvr_time_mode="utc")
    await client.probe_clock()
    with pytest.raises(DvrAuthError):
        await client.search(
            101,
            datetime(2026, 7, 24, 13, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 24, 14, 0, tzinfo=timezone.utc),
        )
    await client.aclose()


@respx.mock
async def test_connect_error_becomes_unreachable():
    respx.post("http://10.10.11.56:80/ISAPI/ContentMgmt/search").mock(
        side_effect=httpx.ConnectError("no route to host")
    )
    client = make_client(dvr_time_mode="utc")
    await client.probe_clock()
    with pytest.raises(DvrUnreachable):
        await client.search(
            101,
            datetime(2026, 7, 24, 13, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 24, 14, 0, tzinfo=timezone.utc),
        )
    await client.aclose()


@respx.mock
async def test_stream_download_yields_bytes_and_escapes_uri():
    route = respx.post("http://10.10.11.56:80/ISAPI/ContentMgmt/download").mock(
        return_value=httpx.Response(200, content=b"IMKH" + b"\x00" * 1000)
    )
    client = make_client(dvr_time_mode="utc")
    uri = "rtsp://10.10.11.56/Streaming/tracks/101/?starttime=20260724T132029Z&size=1"
    chunks = [c async for c in client.stream_download(uri)]
    assert b"".join(chunks).startswith(b"IMKH")
    sent = route.calls[0].request.content.decode()
    assert "&amp;size=1" in sent, "the playbackURI must be XML-escaped in the body"
    await client.aclose()


@respx.mock
async def test_download_5xx_becomes_busy():
    respx.post("http://10.10.11.56:80/ISAPI/ContentMgmt/download").mock(
        return_value=httpx.Response(503, text="busy")
    )
    client = make_client(dvr_time_mode="utc")
    with pytest.raises(DvrBusy):
        async for _ in client.stream_download("rtsp://10.10.11.56/Streaming/tracks/101/?a=1"):
            pass
    await client.aclose()
