"""/api/stream-raw — unmuxed DVR bytes for client-side remuxing, plus SD/HD.

Two things are being pinned here.

1. /api/stream-raw proxies the DVR's MPEG-PS through untouched: no ffmpeg on the
   server, no staging. It is what makes client-side remux faster than the existing
   path. It must still behave like every other DVR-fetching endpoint where it
   counts — same concurrency budget, same 503/502/401 mapping, same 410 for an
   expired clip id — because it competes for the same limited DVR connections.

2. Both playback endpoints take ?quality=sd|hd. SD is the substream (fast), HD the
   mainstream (full quality). The v0.1.3 fallback — a channel with no substream
   recorded still plays, via the mainstream, with a log line — must survive
   unchanged, and /api/download must stay full-quality regardless of either.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from hikvision_recordings.app import main as main_module
from hikvision_recordings.app.config import load_config
from hikvision_recordings.app.isapi import (
    DvrAuthError,
    DvrBusy,
    DvrUnreachable,
    Recording,
)
from hikvision_recordings.app.main import create_app

MAIN_START = datetime(2026, 7, 25, 21, 14, 0, tzinfo=timezone.utc)
MAIN_END = MAIN_START + timedelta(seconds=35)

MAIN_CLIP = Recording(
    channel=101,
    start=MAIN_START,
    end=MAIN_END,
    size_bytes=17_800_000,
    playback_uri="rtsp://10.10.11.56/Streaming/tracks/101/?starttime=20260725T211400Z&size=17800000",
)
SUB_CLIP = Recording(
    channel=102,
    start=MAIN_START + timedelta(seconds=2),
    end=MAIN_END - timedelta(seconds=1),
    size_bytes=10_500_000,
    playback_uri="rtsp://10.10.11.56/Streaming/tracks/102/?starttime=20260725T211402Z&size=10500000",
)

# 'IMKH' is the real Hikvision MPEG-PS magic, so these double as a check that the
# bytes reaching the client are the container ffmpeg.wasm expects to remux.
MAIN_PAYLOAD = b"IMKH" + b"m" * 4000
SUB_PAYLOAD = b"IMKH" + b"s" * 2000

OPTIONS = {
    "dvr_host": "10.10.11.56",
    "dvr_username": "Jimmy",
    "dvr_password": "s3cret",
    "channels": [{"id": 101, "name": "DriveWay1"}],
}


class FakeClient:
    def __init__(self, substream_results=None, stream_error=None):
        self.substream_results = (
            substream_results if substream_results is not None else [SUB_CLIP]
        )
        self.stream_error = stream_error
        self.searched_channels: list[int] = []
        self.downloaded_uris: list[str] = []
        self.clock = SimpleNamespace(offset=timedelta(0), source="configured",
                                     drift=timedelta(0))

    async def probe_clock(self):
        return self.clock

    async def ping(self):
        return None

    async def search(self, channel, start_utc, end_utc, max_results=None):
        self.searched_channels.append(channel)
        if channel == 102:
            return list(self.substream_results)
        return [MAIN_CLIP]

    async def stream_download(self, playback_uri, chunk_size=262144):
        self.downloaded_uris.append(playback_uri)
        if self.stream_error:
            raise self.stream_error
        yield SUB_PAYLOAD if "/tracks/102/" in playback_uri else MAIN_PAYLOAD

    async def aclose(self):
        return None


@pytest.fixture
def no_remux(monkeypatch):
    """Fail loudly if the raw path ever reaches the server-side remuxer."""
    def _boom(_source):
        raise AssertionError("/api/stream-raw ran ffmpeg on the server")
    monkeypatch.setattr(main_module, "remux_to_fmp4", _boom)


def _client(fake: FakeClient) -> TestClient:
    return TestClient(create_app(load_config(OPTIONS), client=fake))


def _clip_id(client: TestClient) -> str:
    body = client.get(
        "/api/recordings",
        params={"channel": 101, "start": "2026-07-25T21:00:00Z", "end": "2026-07-25T22:00:00Z"},
    ).json()
    return body["clips"][0]["id"]


# ── raw passthrough ──────────────────────────────────────────────────────────

def test_raw_returns_the_dvr_bytes_unmuxed(no_remux):
    """The response must be the DVR's own MPEG-PS, byte for byte."""
    fake = FakeClient()
    with _client(fake) as client:
        response = client.get(f"/api/stream-raw/{_clip_id(client)}")
    assert response.status_code == 200
    assert response.content == SUB_PAYLOAD
    assert response.content.startswith(b"IMKH"), "not the raw Hikvision container"


def test_raw_is_not_advertised_as_mp4(no_remux):
    """It isn't an MP4 yet — mislabelling it would let a <video> try to play it."""
    fake = FakeClient()
    with _client(fake) as client:
        response = client.get(f"/api/stream-raw/{_clip_id(client)}")
    assert response.headers["content-type"].startswith("video/mpeg")
    assert "mp4" not in response.headers["content-type"]


def test_raw_matches_the_bytes_download_would_give_for_the_same_track(no_remux):
    """Raw playback and raw download must agree — same source, same bytes."""
    fake = FakeClient()
    with _client(fake) as client:
        clip = _clip_id(client)
        streamed = client.get(f"/api/stream-raw/{clip}", params={"quality": "hd"})
        downloaded = client.get(f"/api/download/{clip}", params={"raw": 1})
    assert streamed.content == downloaded.content == MAIN_PAYLOAD


# ── same DVR-failure semantics as every other endpoint ───────────────────────

@pytest.mark.parametrize(
    "error, status",
    [
        (DvrBusy("DVR is busy serving another stream."), 503),
        (DvrUnreachable("Can't reach the DVR."), 502),
        (DvrAuthError("The DVR rejected the username or password."), 401),
    ],
)
def test_raw_maps_dvr_failures_like_the_other_endpoints(error, status, no_remux):
    fake = FakeClient(stream_error=error)
    with _client(fake) as client:
        response = client.get(f"/api/stream-raw/{_clip_id(client)}")
    assert response.status_code == status


def test_busy_dvr_says_busy_rather_than_returning_an_empty_200(no_remux):
    """A 200 with zero bytes would give the client a silently dead player."""
    fake = FakeClient(stream_error=DvrBusy("DVR is busy serving another stream."))
    with _client(fake) as client:
        response = client.get(f"/api/stream-raw/{_clip_id(client)}")
    assert response.status_code == 503
    assert "busy" in response.json()["detail"].lower()


def test_raw_goes_through_the_shared_connection_budget(no_remux):
    """It must use stream_download, which is what holds the semaphore."""
    fake = FakeClient()
    with _client(fake) as client:
        client.get(f"/api/stream-raw/{_clip_id(client)}")
    assert len(fake.downloaded_uris) == 1, "bypassed the concurrency guard"


def test_expired_clip_id_returns_410_like_the_other_endpoints(no_remux):
    fake = FakeClient()
    with _client(fake) as client:
        assert client.get("/api/stream-raw/doesnotexist").status_code == 410


# ── quality selection, on both playback paths ────────────────────────────────

@pytest.mark.parametrize("endpoint", ["api/stream", "api/stream-raw"])
def test_sd_is_the_default_quality(endpoint, monkeypatch):
    monkeypatch.setattr(main_module, "remux_to_fmp4", lambda src: src)
    fake = FakeClient()
    with _client(fake) as client:
        response = client.get(f"/{endpoint}/{_clip_id(client)}")
    assert response.content == SUB_PAYLOAD, "default quality was not the substream"


@pytest.mark.parametrize("endpoint", ["api/stream", "api/stream-raw"])
def test_hd_serves_the_mainstream(endpoint, monkeypatch):
    monkeypatch.setattr(main_module, "remux_to_fmp4", lambda src: src)
    fake = FakeClient()
    with _client(fake) as client:
        response = client.get(f"/{endpoint}/{_clip_id(client)}", params={"quality": "hd"})
    assert response.content == MAIN_PAYLOAD
    assert any("/tracks/101/" in u for u in fake.downloaded_uris)


@pytest.mark.parametrize("endpoint", ["api/stream", "api/stream-raw"])
def test_hd_does_not_waste_a_substream_search(endpoint, monkeypatch):
    """HD already holds the recording it wants; a second search is pure latency."""
    monkeypatch.setattr(main_module, "remux_to_fmp4", lambda src: src)
    fake = FakeClient()
    with _client(fake) as client:
        client.get(f"/{endpoint}/{_clip_id(client)}", params={"quality": "hd"})
    assert 102 not in fake.searched_channels


@pytest.mark.parametrize("endpoint", ["api/stream", "api/stream-raw"])
def test_sd_falls_back_to_mainstream_when_no_substream_was_recorded(endpoint, monkeypatch):
    """The v0.1.3 safety net: asking for SD is a preference, not a precondition.

    A channel with no substream must still play — cleanly, via the mainstream —
    rather than becoming a new failure mode for an explicit SD request.
    """
    monkeypatch.setattr(main_module, "remux_to_fmp4", lambda src: src)
    fake = FakeClient(substream_results=[])
    with _client(fake) as client:
        response = client.get(f"/{endpoint}/{_clip_id(client)}", params={"quality": "sd"})
    assert response.status_code == 200
    assert response.content == MAIN_PAYLOAD
    assert 102 in fake.searched_channels, "never even looked for a substream"


@pytest.mark.parametrize("endpoint", ["api/stream", "api/stream-raw"])
@pytest.mark.parametrize("bad", ["4k", "", "SD ", "hd;sd", "mainstream"])
def test_unknown_quality_is_rejected(endpoint, bad, monkeypatch):
    monkeypatch.setattr(main_module, "remux_to_fmp4", lambda src: src)
    fake = FakeClient()
    with _client(fake) as client:
        response = client.get(f"/{endpoint}/{_clip_id(client)}", params={"quality": bad})
    assert response.status_code == 400
    assert not fake.downloaded_uris, "hit the DVR before validating the request"


@pytest.mark.parametrize("quality", ["sd", "hd"])
def test_download_ignores_quality_and_stays_on_the_mainstream(quality, monkeypatch):
    """Download is always full quality, whatever the user picked for playback."""
    monkeypatch.setattr(main_module, "remux_to_fmp4", lambda src: src)
    fake = FakeClient()
    with _client(fake) as client:
        response = client.get(f"/api/download/{_clip_id(client)}", params={"quality": quality})
    assert response.status_code == 200
    assert response.content == MAIN_PAYLOAD, "download regressed to the substream"
    assert not any("/tracks/102/" in u for u in fake.downloaded_uris)
