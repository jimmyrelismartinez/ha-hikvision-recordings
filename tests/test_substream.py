"""Playback uses the DVR's substream; download stays on the mainstream.

Hikvision track numbering is channel*100 + streamType (1=main, 2=sub). Since the
Range fix means a clip is fully fetched and remuxed before playback can start,
serving the smaller substream roughly halves time-to-play. Measured live on this
DVR for the same 35 s clip: mainstream 17.8 MB / 7.4 s, substream 10.5 MB / 3.4 s.

Download must keep full 1920x1080 mainstream quality, and a channel with no
substream recording must still play — so the fallback is a real safety net, not
decoration. All three are pinned here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from hikvision_recordings.app import main as main_module
from hikvision_recordings.app.config import load_config
from hikvision_recordings.app.isapi import DvrUnreachable, Recording
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
# Substream of the same event: starts a couple of seconds off, as real ones do.
SUB_CLIP = Recording(
    channel=102,
    start=MAIN_START + timedelta(seconds=2),
    end=MAIN_END - timedelta(seconds=1),
    size_bytes=10_500_000,
    playback_uri="rtsp://10.10.11.56/Streaming/tracks/102/?starttime=20260725T211402Z&size=10500000",
)
# A neighbouring substream event that the padded search window also catches; it
# must lose to SUB_CLIP on overlap.
SUB_NEIGHBOUR = Recording(
    channel=102,
    start=MAIN_START - timedelta(seconds=28),
    end=MAIN_START - timedelta(seconds=3),
    size_bytes=5_000_000,
    playback_uri="rtsp://10.10.11.56/Streaming/tracks/102/?starttime=20260725T211332Z&size=5000000",
)

MAIN_PAYLOAD = b"MAINSTREAM-" + b"m" * 4000
SUB_PAYLOAD = b"SUBSTREAM-" + b"s" * 2000

OPTIONS = {
    "dvr_host": "10.10.11.56",
    "dvr_username": "Jimmy",
    "dvr_password": "s3cret",
    "channels": [{"id": 101, "name": "DriveWay1"}],
}


class FakeClient:
    """Serves distinguishable bytes per track so we can tell which one was fetched."""

    def __init__(self, substream_results=None, substream_error=None):
        self.substream_results = (
            substream_results if substream_results is not None else [SUB_NEIGHBOUR, SUB_CLIP]
        )
        self.substream_error = substream_error
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
            if self.substream_error:
                raise self.substream_error
            return list(self.substream_results)
        return [MAIN_CLIP]

    async def stream_download(self, playback_uri, chunk_size=262144):
        self.downloaded_uris.append(playback_uri)
        yield SUB_PAYLOAD if "/tracks/102/" in playback_uri else MAIN_PAYLOAD

    async def aclose(self):
        return None


@pytest.fixture(autouse=True)
def _staging(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "STAGING_DIR", tmp_path)
    # Playback normally remuxes; identity keeps these tests about track selection.
    monkeypatch.setattr(main_module, "remux_to_fmp4", lambda src: src)


def _client(fake: FakeClient) -> TestClient:
    return TestClient(create_app(load_config(OPTIONS), client=fake))


def _clip_id(client: TestClient) -> str:
    body = client.get(
        "/api/recordings",
        params={"channel": 101, "start": "2026-07-25T21:00:00Z", "end": "2026-07-25T22:00:00Z"},
    ).json()
    return body["clips"][0]["id"]


def test_stream_fetches_the_substream_track():
    fake = FakeClient()
    with _client(fake) as client:
        response = client.get(f"/api/stream/{_clip_id(client)}")
    assert response.status_code == 200
    assert response.content == SUB_PAYLOAD, "playback fetched the mainstream, not the substream"
    assert 102 in fake.searched_channels, "never searched the substream track"
    assert any("/tracks/102/" in u for u in fake.downloaded_uris)
    assert not any("/tracks/101/" in u for u in fake.downloaded_uris)


def test_stream_picks_the_best_overlapping_substream_not_just_the_first():
    """A padded window also returns the neighbouring event — overlap must decide."""
    fake = FakeClient(substream_results=[SUB_NEIGHBOUR, SUB_CLIP])
    with _client(fake) as client:
        client.get(f"/api/stream/{_clip_id(client)}")
    used = [u for u in fake.downloaded_uris if "/tracks/102/" in u]
    assert used, "no substream fetched"
    assert "20260725T211402Z" in used[0], "picked the neighbouring clip, not the overlapping one"


def test_falls_back_to_mainstream_when_no_substream_exists():
    """A channel recorded without a substream must still play."""
    fake = FakeClient(substream_results=[])
    with _client(fake) as client:
        response = client.get(f"/api/stream/{_clip_id(client)}")
    assert response.status_code == 200
    assert response.content == MAIN_PAYLOAD
    assert any("/tracks/101/" in u for u in fake.downloaded_uris)


def test_falls_back_to_mainstream_when_the_substream_search_errors():
    fake = FakeClient(substream_error=DvrUnreachable("Can't reach the DVR."))
    with _client(fake) as client:
        response = client.get(f"/api/stream/{_clip_id(client)}")
    assert response.status_code == 200, "a substream hiccup must not break playback"
    assert response.content == MAIN_PAYLOAD


def test_falls_back_when_the_only_candidate_does_not_overlap():
    fake = FakeClient(substream_results=[SUB_NEIGHBOUR])
    with _client(fake) as client:
        response = client.get(f"/api/stream/{_clip_id(client)}")
    assert response.content == MAIN_PAYLOAD


def test_download_still_uses_the_mainstream_unchanged():
    """Full quality on download is the whole point of splitting the two paths."""
    fake = FakeClient()
    with _client(fake) as client:
        response = client.get(f"/api/download/{_clip_id(client)}")
    assert response.status_code == 200
    assert response.content == MAIN_PAYLOAD, "download regressed to the substream"
    assert any("/tracks/101/" in u for u in fake.downloaded_uris)
    assert not any("/tracks/102/" in u for u in fake.downloaded_uris)
    assert 102 not in fake.searched_channels, "download should not even look up a substream"


def test_recordings_list_and_thumbnails_stay_on_the_mainstream():
    """Listing/thumbnails must describe what Download gives you, so: mainstream."""
    fake = FakeClient()
    with _client(fake) as client:
        clip = _clip_id(client)
        client.get(f"/api/thumbnail/{clip}")
    assert fake.searched_channels == [101], "listing/thumbnail touched a substream track"
    assert all("/tracks/101/" in u for u in fake.downloaded_uris)


def test_substream_uri_from_a_foreign_host_is_rejected_and_falls_back():
    """The substream URI bypasses registry.put(), so it must still be validated."""
    hostile = Recording(
        channel=102,
        start=SUB_CLIP.start,
        end=SUB_CLIP.end,
        size_bytes=1,
        playback_uri="rtsp://evil.example.com/Streaming/tracks/102/?starttime=1&size=1",
    )
    fake = FakeClient(substream_results=[hostile])
    with _client(fake) as client:
        response = client.get(f"/api/stream/{_clip_id(client)}")
    assert response.content == MAIN_PAYLOAD, "fetched a substream URI from a foreign host"
    assert not any("evil.example.com" in u for u in fake.downloaded_uris)
