from datetime import datetime, timezone

import pytest

from hikvision_recordings.app.clips import KIND_DVR, KIND_REOLINK, Clip
from hikvision_recordings.app.registry import (
    ClipExpired,
    ClipRegistry,
    InvalidPlaybackUri,
)


def make_recording(uri: str = "rtsp://192.0.2.56/Streaming/tracks/101/?starttime=1&size=2",
                   kind: str = KIND_DVR, sub: str | None = None):
    return Clip(
        camera_key="dvr-101" if kind == KIND_DVR else "reolink-0",
        kind=kind,
        channel=101 if kind == KIND_DVR else 0,
        start=datetime(2026, 7, 24, 18, 20, 29, tzinfo=timezone.utc),
        end=datetime(2026, 7, 24, 18, 21, 42, tzinfo=timezone.utc),
        size_bytes=32947084,
        main_ref=uri,
        sub_ref=sub,
    )


def test_put_returns_opaque_id_and_get_round_trips():
    reg = ClipRegistry(dvr_host="192.0.2.56")
    clip_id = reg.put(make_recording())
    assert len(clip_id) >= 12
    assert "rtsp" not in clip_id, "the id must not leak the URI to the browser"
    assert reg.get(clip_id).main_ref.startswith("rtsp://192.0.2.56/")


def test_unknown_id_raises_expired():
    reg = ClipRegistry(dvr_host="192.0.2.56")
    with pytest.raises(ClipExpired):
        reg.get("nope")


def test_entries_expire():
    reg = ClipRegistry(dvr_host="192.0.2.56", ttl_s=0)
    clip_id = reg.put(make_recording())
    with pytest.raises(ClipExpired):
        reg.get(clip_id)


def test_capacity_evicts_oldest():
    reg = ClipRegistry(dvr_host="192.0.2.56", capacity=2)
    first = reg.put(make_recording(
        "rtsp://192.0.2.56/Streaming/tracks/101/?starttime=1&size=1"))
    reg.put(make_recording(
        "rtsp://192.0.2.56/Streaming/tracks/101/?starttime=2&size=2"))
    reg.put(make_recording(
        "rtsp://192.0.2.56/Streaming/tracks/101/?starttime=3&size=3"))
    assert len(reg) == 2
    with pytest.raises(ClipExpired):
        reg.get(first)


def test_same_clip_twice_reuses_id_and_refreshes_ttl():
    """Infinite-scroll re-list must not mint a fresh id (design §10.1)."""
    reg = ClipRegistry(dvr_host="192.0.2.56", ttl_s=60.0)
    clip = make_recording()
    first = reg.put(clip)
    # Advance "time" by patching the stored expiry into the past-ish future,
    # then put again and confirm the id is reused and the entry is still live.
    entry = reg._entries[first]
    entry.expires_at = entry.expires_at - 30.0
    before = entry.expires_at
    second = reg.put(clip.with_sub(
        "rtsp://192.0.2.56/Streaming/tracks/102/?starttime=1&size=2", 100))
    assert second == first
    assert len(reg) == 1
    updated = reg.get(first)
    assert updated.sub_ref is not None
    assert reg._entries[first].expires_at > before


def test_eviction_removes_identity_index():
    reg = ClipRegistry(dvr_host="192.0.2.56", capacity=1)
    a = make_recording("rtsp://192.0.2.56/Streaming/tracks/101/?starttime=1&size=1")
    b = make_recording("rtsp://192.0.2.56/Streaming/tracks/101/?starttime=2&size=2")
    first = reg.put(a)
    reg.put(b)
    with pytest.raises(ClipExpired):
        reg.get(first)
    # Re-putting A after eviction must mint a NEW id (index entry was cleared).
    again = reg.put(a)
    assert again != first


def test_foreign_host_uri_is_rejected():
    reg = ClipRegistry(dvr_host="192.0.2.56")
    with pytest.raises(InvalidPlaybackUri):
        reg.put(make_recording(uri="rtsp://evil.example.com/Streaming/tracks/101/?a=1"))


def test_non_rtsp_uri_is_rejected():
    reg = ClipRegistry(dvr_host="192.0.2.56")
    with pytest.raises(InvalidPlaybackUri):
        reg.put(make_recording(uri="http://192.0.2.56/etc/passwd"))


def test_traversal_in_query_string_is_rejected():
    """Path traversal attack in the query string must be rejected."""
    reg = ClipRegistry(dvr_host="192.0.2.56")
    with pytest.raises(InvalidPlaybackUri):
        reg.put(make_recording(uri="rtsp://192.0.2.56/Streaming/tracks/101/?a=1/../../../etc/passwd"))


def test_crlf_in_query_string_is_rejected():
    """CRLF injection in query string must be rejected."""
    reg = ClipRegistry(dvr_host="192.0.2.56")
    with pytest.raises(InvalidPlaybackUri):
        reg.put(make_recording(uri="rtsp://192.0.2.56/Streaming/tracks/101/?a=1\r\nEvil-Header: 1"))


def test_nul_in_query_string_is_rejected():
    """NUL byte injection in query string must be rejected."""
    reg = ClipRegistry(dvr_host="192.0.2.56")
    with pytest.raises(InvalidPlaybackUri):
        reg.put(make_recording(uri="rtsp://192.0.2.56/Streaming/tracks/101/?a=1\x00.evil.com"))


def test_real_playback_uri_from_hikvision_still_works():
    """Real URIs from Hikvision search response must work."""
    reg = ClipRegistry(dvr_host="192.0.2.56")
    real_uri = "rtsp://192.0.2.56/Streaming/tracks/101/?starttime=20260724T132029Z&endtime=20260724T132142Z&name=00000000002000001&size=32947084"
    clip_id = reg.put(make_recording(uri=real_uri))
    assert reg.get(clip_id).main_ref == real_uri


def test_error_message_names_both_hosts():
    """Error message must name both expected and actual hosts for diagnostics."""
    reg = ClipRegistry(dvr_host="192.0.2.56")
    with pytest.raises(InvalidPlaybackUri) as exc_info:
        reg.put(make_recording(uri="rtsp://dvr.local/Streaming/tracks/101/?a=1"))
    msg = str(exc_info.value)
    assert "dvr.local" in msg, "error must name the actual host in the URI"
    assert "192.0.2.56" in msg, "error must name the configured dvr_host"
    assert "set dvr_host to that IP" in msg, "error must hint at the solution"


# ── v0.2.0: Reolink refs and substream refs ──────────────────────────────────

REOLINK_MAIN = "Mp4Record/2026-11-14/RecM0A_20261114_183508_183650_0_9B28C080000000_41D8273.mp4"
REOLINK_SUB = "Mp4Record/2026-11-14/RecS0A_20261114_183506_183650_0_5514C080000000_65A424.mp4"


def test_real_reolink_refs_are_accepted():
    reg = ClipRegistry()
    clip_id = reg.put(make_recording(REOLINK_MAIN, kind=KIND_REOLINK, sub=REOLINK_SUB))
    assert reg.get(clip_id).main_ref == REOLINK_MAIN
    assert reg.get(clip_id).sub_ref == REOLINK_SUB


def test_reolink_dst_ref_is_accepted():
    reg = ClipRegistry()
    reg.put(make_recording(
        "Mp4Record/2026-11-01/RecM0A_DST20261101_003727_003751_0_9B288100000000_1A2B.mp4",
        kind=KIND_REOLINK))


@pytest.mark.parametrize("ref", [
    "Mp4Record/2026-11-14/../../etc/passwd",
    "Mp4Record/2026-11-14/RecM0A_20261114_183508_183650_0_9B28&cmd=Reboot.mp4",
    "Mp4Record/2026-11-14/RecM0A_20261114_183508_183650_0_9B28%26x.mp4",
    "Mp4Record/2026-11-14/RecM0A_20261114_183508_183650_0_9B28.mp4\r\nX: 1",
    "/Mp4Record/2026-11-14/RecM0A_20261114_183508_183650_0_9B28.mp4",
    "http://evil/RecM0A_20261114_183508_183650_0_9B28.mp4",
    "rtsp://192.0.2.56/Streaming/tracks/101/?a=1",
])
def test_hostile_reolink_refs_are_rejected(ref):
    reg = ClipRegistry(dvr_host="192.0.2.56")
    with pytest.raises(InvalidPlaybackUri):
        reg.put(make_recording(ref, kind=KIND_REOLINK))


def test_reolink_ref_is_not_accepted_as_a_dvr_ref():
    reg = ClipRegistry(dvr_host="192.0.2.56")
    with pytest.raises(InvalidPlaybackUri):
        reg.put(make_recording(REOLINK_MAIN, kind=KIND_DVR))


def test_no_dvr_configured_rejects_every_dvr_ref():
    reg = ClipRegistry()
    with pytest.raises(InvalidPlaybackUri):
        reg.put(make_recording())


def test_invalid_sub_ref_is_dropped_not_fatal():
    """A bad substream must not cost the user the clip; it plays from main."""
    reg = ClipRegistry(dvr_host="192.0.2.56")
    clip_id = reg.put(make_recording(sub="rtsp://evil.example.com/Streaming/tracks/102/?a=1"))
    assert reg.get(clip_id).sub_ref is None
