from datetime import datetime, timezone

import pytest

from hikvision_recordings.app.isapi import Recording
from hikvision_recordings.app.registry import (
    ClipExpired,
    ClipRegistry,
    InvalidPlaybackUri,
)


def make_recording(uri: str = "rtsp://10.10.11.56/Streaming/tracks/101/?starttime=1&size=2"):
    return Recording(
        channel=101,
        start=datetime(2026, 7, 24, 18, 20, 29, tzinfo=timezone.utc),
        end=datetime(2026, 7, 24, 18, 21, 42, tzinfo=timezone.utc),
        size_bytes=32947084,
        playback_uri=uri,
    )


def test_put_returns_opaque_id_and_get_round_trips():
    reg = ClipRegistry(dvr_host="10.10.11.56")
    clip_id = reg.put(make_recording())
    assert len(clip_id) >= 12
    assert "rtsp" not in clip_id, "the id must not leak the URI to the browser"
    assert reg.get(clip_id).playback_uri.startswith("rtsp://10.10.11.56/")


def test_unknown_id_raises_expired():
    reg = ClipRegistry(dvr_host="10.10.11.56")
    with pytest.raises(ClipExpired):
        reg.get("nope")


def test_entries_expire():
    reg = ClipRegistry(dvr_host="10.10.11.56", ttl_s=0)
    clip_id = reg.put(make_recording())
    with pytest.raises(ClipExpired):
        reg.get(clip_id)


def test_capacity_evicts_oldest():
    reg = ClipRegistry(dvr_host="10.10.11.56", capacity=2)
    first = reg.put(make_recording())
    reg.put(make_recording())
    reg.put(make_recording())
    assert len(reg) == 2
    with pytest.raises(ClipExpired):
        reg.get(first)


def test_foreign_host_uri_is_rejected():
    reg = ClipRegistry(dvr_host="10.10.11.56")
    with pytest.raises(InvalidPlaybackUri):
        reg.put(make_recording(uri="rtsp://evil.example.com/Streaming/tracks/101/?a=1"))


def test_non_rtsp_uri_is_rejected():
    reg = ClipRegistry(dvr_host="10.10.11.56")
    with pytest.raises(InvalidPlaybackUri):
        reg.put(make_recording(uri="http://10.10.11.56/etc/passwd"))
