"""v0.2.0 API: DVR + Reolink cameras side by side, string camera keys,
all-cameras search with partial failure, direct (no-remux) Reolink playback,
quality-aware download, and DVR time-cursor paging."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import hikvision_recordings.app.main as main_module
from hikvision_recordings.app.clips import KIND_REOLINK, Clip
from hikvision_recordings.app.config import ReolinkCamera, load_config
from hikvision_recordings.app.isapi import (
    ClipGone,
    DvrUnreachable,
    RangeNotSatisfiable,
    Recording,
)
from hikvision_recordings.app.main import create_app
from hikvision_recordings.app.reolink import Upstream

T0 = datetime(2026, 11, 14, 18, 0, 0, tzinfo=timezone.utc)
MAIN_REF = "Mp4Record/2026-11-14/RecM0A_20261114_120000_120030_0_9B28C080000000_41D8273.mp4"
SUB_REF = "Mp4Record/2026-11-14/RecS0A_20261114_120000_120030_0_5514C080000000_65A424.mp4"

DVR_OPTIONS = {
    "dvr_host": "192.0.2.56",
    "dvr_username": "u",
    "dvr_password": "p",
    "channels": [{"id": 101, "name": "Driveway"}],
    "max_results": 40,
}
REOLINK_ROW = {"name": "Porch", "host": "192.0.2.40", "username": "v", "password": "pw"}


def rec(minute: int, channel: int = 101) -> Recording:
    start = T0 + timedelta(minutes=minute)
    return Recording(
        channel=channel, start=start, end=start + timedelta(seconds=30), size_bytes=1000,
        playback_uri=f"rtsp://192.0.2.56/Streaming/tracks/{channel}/?starttime={minute}&size=1000",
    )


class FakeDvr:
    def __init__(self, recordings=None, error=None):
        self.recordings = recordings if recordings is not None else [rec(5)]
        self.error = error
        self.searches = []
        self.clock = SimpleNamespace(offset=timedelta(0), source="configured", drift=timedelta(0))

    async def probe_clock(self):
        return self.clock

    async def ping(self):
        if self.error:
            raise self.error

    async def search(self, channel, start_utc, end_utc, max_results=None):
        self.searches.append((channel, start_utc, max_results))
        if self.error:
            raise self.error
        if channel % 2 == 0:
            return []
        # Like the real DVR: oldest first, at most one page from the cursor.
        hits = sorted((r for r in self.recordings if r.end >= start_utc and r.start <= end_utc),
                      key=lambda r: r.start)
        return hits[: max_results or 40]

    async def stream_download(self, playback_uri, chunk_size=262144):
        yield b"IMKH"

    async def aclose(self):
        pass


class FakeReolink:
    kind = KIND_REOLINK

    def __init__(self, clips=None, error=None, open_error=None, payload=b"MP4" * 100):
        self.key = "reolink-0"
        self.camera = ReolinkCamera(**REOLINK_ROW)
        self.clock = SimpleNamespace(offset=timedelta(hours=-6), source="measured",
                                     drift=timedelta(0))
        self.clips = clips if clips is not None else [Clip(
            camera_key=self.key, kind=KIND_REOLINK, channel=0,
            start=T0 + timedelta(minutes=10), end=T0 + timedelta(minutes=10, seconds=30),
            size_bytes=5000, main_ref=MAIN_REF, sub_ref=SUB_REF, sub_size_bytes=900)]
        self.error = error
        self.open_error = open_error
        self.payload = payload
        self.opened = []
        self.closed = 0

    async def probe_clock(self):
        return self.clock

    async def ping(self):
        if self.error:
            raise self.error

    async def search(self, start_utc, end_utc, limit):
        if self.error:
            raise self.error
        return list(self.clips), False

    async def open(self, ref, range_header=None, clamp=False):
        self.opened.append((ref, range_header, clamp))
        if self.open_error:
            raise self.open_error

        async def body():
            yield self.payload

        async def closer():
            self.closed += 1

        status = 206 if range_header else 200
        headers = {"content-length": str(len(self.payload))}
        if range_header:
            headers["content-range"] = f"bytes 0-{len(self.payload) - 1}/{len(self.payload)}"
        return Upstream(status=status, headers=headers, body=body(), _closer=closer)

    async def aclose(self):
        pass


def app_client(options, dvr=None, reolink=None) -> TestClient:
    config = load_config(options)
    return TestClient(create_app(config, client=dvr,
                                 reolink_clients=[reolink] if reolink else []))


BOTH = {**DVR_OPTIONS, "reolink_cameras": [REOLINK_ROW]}
WINDOW = {"start": "2026-11-14T17:00:00Z", "end": "2026-11-14T20:00:00Z"}


def search(client, **params):
    return client.get("/api/recordings", params={**WINDOW, **params})


# ── cameras & health ─────────────────────────────────────────────────────────

def test_channels_list_every_camera_by_key():
    with app_client(BOTH, FakeDvr(), FakeReolink()) as client:
        body = client.get("/api/channels").json()
    assert [(c["key"], c["kind"], c["name"]) for c in body] == [
        ("dvr-101", "dvr", "Driveway"), ("reolink-0", "reolink", "Porch")]


def test_reolink_only_install_needs_no_dvr():
    options = {"reolink_cameras": [REOLINK_ROW]}
    with app_client(options, None, FakeReolink()) as client:
        health = client.get("/api/health").json()
        clips = search(client, camera="all").json()["clips"]
    assert health["dvr"] == "not_configured"
    assert [d["state"] for d in health["devices"]] == ["ok"]
    assert [c["camera_key"] for c in clips] == ["reolink-0"]


def test_health_reports_each_device():
    with app_client(BOTH, FakeDvr(), FakeReolink(error=DvrUnreachable("x"))) as client:
        health = client.get("/api/health").json()
    assert health["dvr"] == "ok"
    assert [(d["prefix"], d["state"]) for d in health["devices"]] == [
        ("dvr", "ok"), ("reolink-0", "unreachable")]


# ── search ───────────────────────────────────────────────────────────────────

def test_all_cameras_merge_newest_first_with_labels():
    with app_client(BOTH, FakeDvr([rec(5), rec(20)]), FakeReolink()) as client:
        body = search(client, camera="all").json()
    clips = body["clips"]
    assert [c["camera_key"] for c in clips] == ["dvr-101", "reolink-0", "dvr-101"]
    assert [c["start"] for c in clips] == sorted((c["start"] for c in clips), reverse=True)
    reolink = clips[1]
    assert reolink["playback"] == "direct" and reolink["camera_name"] == "Porch"
    assert clips[0]["playback"] == "remux" and clips[0]["channel_name"] == "Driveway"
    assert body["errors"] == [] and body["truncated"] is False
    assert "Mp4Record" not in str(body) and "rtsp" not in str(body)


def test_multi_select_with_repeated_camera_params():
    with app_client(BOTH, FakeDvr(), FakeReolink()) as client:
        clips = client.get("/api/recordings", params=[
            ("camera", "reolink-0"), ("camera", "dvr-101"),
            ("start", WINDOW["start"]), ("end", WINDOW["end"])]).json()["clips"]
    assert {c["camera_key"] for c in clips} == {"dvr-101", "reolink-0"}


def test_one_device_down_still_returns_the_others():
    with app_client(BOTH, FakeDvr(), FakeReolink(error=DvrUnreachable("Can't reach the camera Porch."))) as client:
        response = search(client, camera="all")
    body = response.json()
    assert response.status_code == 200
    assert [c["camera_key"] for c in body["clips"]] == ["dvr-101"]
    assert body["errors"] == [{"camera": "reolink-0", "detail": "Can't reach the camera Porch."}]


def test_every_device_down_is_a_real_error_status():
    with app_client(BOTH, FakeDvr(error=DvrUnreachable("dvr down")),
                    FakeReolink(error=DvrUnreachable("cam down"))) as client:
        assert search(client, camera="all").status_code == 502


def test_legacy_integer_channel_still_works():
    with app_client(BOTH, FakeDvr(), FakeReolink()) as client:
        clips = search(client, channel="101").json()["clips"]
    assert [c["camera_key"] for c in clips] == ["dvr-101"]


@pytest.mark.parametrize("params", [{"camera": "reolink-9"}, {"camera": "dvr-999"}, {}])
def test_unknown_or_missing_camera_is_400(params):
    with app_client(BOTH, FakeDvr(), FakeReolink()) as client:
        assert search(client, **params).status_code == 400


# ── DVR paging by time cursor (verified: searchResultPosition is ignored) ────

def test_dvr_time_cursor_collects_more_than_one_page():
    dvr = FakeDvr([rec(m) for m in range(0, 120)])        # 120 clips, pages of 40
    with app_client({**DVR_OPTIONS}, dvr) as client:
        body = client.get("/api/recordings", params={
            "camera": "dvr-101", "start": "2026-11-14T17:00:00Z",
            "end": "2026-11-14T21:00:00Z"}).json()
    assert len(body["clips"]) == 120 and body["truncated"] is False
    main_searches = [s for s in dvr.searches if s[0] == 101]
    assert len(main_searches) == 4                         # 40 + 40 + 40 + final short page
    assert all(s[2] == 40 for s in main_searches)


def test_dvr_paging_stops_at_max_search_results():
    dvr = FakeDvr([rec(m) for m in range(0, 120)])
    with app_client({**DVR_OPTIONS, "max_search_results": 50}, dvr) as client:
        body = client.get("/api/recordings", params={
            "camera": "dvr-101", "start": "2026-11-14T17:00:00Z",
            "end": "2026-11-14T21:00:00Z"}).json()
    assert len(body["clips"]) == 50
    assert body["truncated"] is True and body["truncated_cameras"] == ["dvr-101"]


def test_page_size_never_exceeds_what_the_dvr_will_return():
    """The DVR caps one search at 64 whatever maxResults says (verified)."""
    dvr = FakeDvr([rec(m) for m in range(0, 10)])
    with app_client({**DVR_OPTIONS, "max_results": 200}, dvr) as client:
        search(client, camera="dvr-101")
    assert all(s[2] <= 50 for s in dvr.searches)


# ── Reolink playback, download, thumbnails ───────────────────────────────────

def _reolink_clip_id(client):
    return search(client, camera="reolink-0").json()["clips"][0]["id"]


def test_reolink_stream_is_direct_with_range_passthrough_and_clamp():
    cam = FakeReolink()
    with app_client(BOTH, FakeDvr(), cam) as client:
        response = client.get(f"/api/stream/{_reolink_clip_id(client)}",
                              headers={"Range": "bytes=0-"})
    assert response.status_code == 206
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-range"].startswith("bytes 0-")
    assert cam.opened == [(SUB_REF, "bytes=0-", True)], "SD default -> substream, clamped"
    assert cam.closed == 1, "upstream not closed after the response"


def test_reolink_stream_hd_uses_the_mainstream():
    cam = FakeReolink()
    with app_client(BOTH, FakeDvr(), cam) as client:
        client.get(f"/api/stream/{_reolink_clip_id(client)}", params={"quality": "hd"})
    assert cam.opened[0][0] == MAIN_REF


def test_reolink_stream_raw_is_not_applicable():
    with app_client(BOTH, FakeDvr(), FakeReolink()) as client:
        assert client.get(f"/api/stream-raw/{_reolink_clip_id(client)}").status_code == 409


def test_reolink_download_is_full_quality_mp4_by_default():
    cam = FakeReolink()
    with app_client(BOTH, FakeDvr(), cam) as client:
        response = client.get(f"/api/download/{_reolink_clip_id(client)}")
    assert response.status_code == 200
    assert 'filename="Porch_' in response.headers["content-disposition"]
    assert response.headers["content-disposition"].endswith('.mp4"')
    assert cam.opened == [(MAIN_REF, None, False)]


def test_reolink_download_sd():
    cam = FakeReolink()
    with app_client(BOTH, FakeDvr(), cam) as client:
        client.get(f"/api/download/{_reolink_clip_id(client)}", params={"quality": "sd"})
    assert cam.opened[0][0] == SUB_REF


@pytest.mark.parametrize("error,status", [
    (ClipGone("That recording is no longer on the camera — search again."), 410),
    (DvrUnreachable("Can't reach the camera Porch."), 502),
])
def test_reolink_errors_map_to_http(error, status):
    with app_client(BOTH, FakeDvr(), FakeReolink(open_error=error)) as client:
        response = client.get(f"/api/stream/{_reolink_clip_id(client)}")
    assert response.status_code == status


def test_reolink_416_carries_content_range():
    cam = FakeReolink(open_error=RangeNotSatisfiable("out", "bytes */300"))
    with app_client(BOTH, FakeDvr(), cam) as client:
        response = client.get(f"/api/stream/{_reolink_clip_id(client)}",
                              headers={"Range": "bytes=9999-"})
    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */300"


def test_reolink_thumbnail_reads_a_bounded_slice_of_the_substream(monkeypatch):
    seen = {}

    async def fake_thumb(source, max_bytes=None):
        seen["bytes"] = b"".join([c async for c in source])
        return b"\xff\xd8jpeg\xff\xd9"

    monkeypatch.setattr(main_module, "thumbnail_from_stream", fake_thumb)
    cam = FakeReolink()
    with app_client(BOTH, FakeDvr(), cam) as client:
        response = client.get(f"/api/thumbnail/{_reolink_clip_id(client)}")
    assert response.status_code == 200
    ref, rng, _ = cam.opened[0]
    assert ref == SUB_REF and rng == f"bytes=0-{main_module.THUMB_MAX_BYTES - 1}"
    assert cam.closed == 1


# ── registry sizing (goldfish B1) ────────────────────────────────────────────

def test_a_large_all_cameras_response_never_evicts_its_own_newest_clips():
    dvr = FakeDvr([rec(m) for m in range(0, 180)])
    cam = FakeReolink(clips=[Clip(
        camera_key="reolink-0", kind=KIND_REOLINK, channel=0,
        start=T0 + timedelta(seconds=i), end=T0 + timedelta(seconds=i + 1),
        size_bytes=1, main_ref=MAIN_REF) for i in range(400)])
    with app_client(BOTH, dvr, cam) as client:
        body = client.get("/api/recordings", params={
            "camera": "all", "start": "2026-11-14T17:00:00Z",
            "end": "2026-11-14T22:00:00Z"}).json()
        newest = body["clips"][0]["id"]
        assert client.get(f"/api/thumbnail/{newest}").status_code != 410
