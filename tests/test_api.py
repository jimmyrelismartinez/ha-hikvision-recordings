from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from hikvision_recordings.app.config import load_config
from hikvision_recordings.app.isapi import (
    DvrAuthError,
    DvrBusy,
    DvrUnreachable,
    Recording,
)
from hikvision_recordings.app.main import create_app

OPTIONS = {
    "dvr_host": "10.10.11.56",
    "dvr_username": "Jimmy",
    "dvr_password": "s3cret",
    "channels": [{"id": 101, "name": "DriveWay1"}, {"id": 401, "name": "ENTRYWAY"}],
    "max_results": 2,
}

RECORDING = Recording(
    channel=101,
    start=datetime(2026, 7, 24, 18, 20, 29, tzinfo=timezone.utc),
    end=datetime(2026, 7, 24, 18, 21, 42, tzinfo=timezone.utc),
    size_bytes=32947084,
    playback_uri="rtsp://10.10.11.56/Streaming/tracks/101/?starttime=20260724T132029Z&size=32947084",
)


class FakeClient:
    """Stands in for IsapiClient. Records calls, returns canned results."""

    def __init__(self, recordings=None, error=None, stream_error=None,
                 payload=b"IMKH-fake-bytes"):
        self.recordings = recordings if recordings is not None else [RECORDING]
        self.error = error              # raised by search() and ping()
        self.stream_error = stream_error  # raised by stream_download() only
        self.payload = payload
        self.calls = []
        self.clock = SimpleNamespace(offset=timedelta(0), source="configured")

    async def probe_clock(self):
        return self.clock

    async def ping(self):
        if self.error:
            raise self.error

    async def search(self, channel, start_utc, end_utc, max_results=None):
        self.calls.append((channel, start_utc, end_utc, max_results))
        if self.error:
            raise self.error
        return list(self.recordings)

    async def stream_download(self, playback_uri, chunk_size=262144):
        if self.stream_error:
            raise self.stream_error
        yield self.payload

    async def aclose(self):
        pass


def client_for(fake: FakeClient, **overrides) -> TestClient:
    app = create_app(load_config({**OPTIONS, **overrides}), client=fake)
    return TestClient(app)


def test_channels_come_from_options():
    with client_for(FakeClient()) as client:
        body = client.get("/api/channels").json()
    assert body == [
        {"id": "dvr-101", "key": "dvr-101", "name": "DriveWay1", "kind": "dvr",
         "snapshot": False},
        {"id": "dvr-401", "key": "dvr-401", "name": "ENTRYWAY", "kind": "dvr",
         "snapshot": False},
    ]


def test_recordings_returns_clips_with_opaque_ids():
    with client_for(FakeClient()) as client:
        response = client.get(
            "/api/recordings",
            params={"channel": 101, "start": "2026-07-24T18:00:00Z", "end": "2026-07-24T19:00:00Z"},
        )
    assert response.status_code == 200
    body = response.json()
    clip = body["clips"][0]
    assert clip["duration_s"] == 73
    assert clip["size_bytes"] == 32947084
    assert "rtsp" not in str(body), "playbackURI must never reach the browser"
    assert len(clip["id"]) >= 12


def test_recordings_flags_truncation_at_max_results():
    fake = FakeClient(recordings=[RECORDING, RECORDING])
    with client_for(fake) as client:
        body = client.get(
            "/api/recordings",
            params={"channel": 101, "start": "2026-07-24T18:00:00Z", "end": "2026-07-24T19:00:00Z"},
        ).json()
    assert body["truncated"] is True


def test_unknown_channel_is_rejected():
    with client_for(FakeClient()) as client:
        response = client.get(
            "/api/recordings",
            params={"channel": 999, "start": "2026-07-24T18:00:00Z", "end": "2026-07-24T19:00:00Z"},
        )
    assert response.status_code == 400


def test_end_before_start_is_rejected():
    with client_for(FakeClient()) as client:
        response = client.get(
            "/api/recordings",
            params={"channel": 101, "start": "2026-07-24T19:00:00Z", "end": "2026-07-24T18:00:00Z"},
        )
    assert response.status_code == 400


@pytest.mark.parametrize(
    "error,status,fragment",
    [
        (DvrUnreachable("Can't reach the DVR at 10.10.11.56."), 502, "reach the DVR"),
        (DvrAuthError("The DVR rejected the username or password."), 401, "rejected the username"),
        (DvrBusy("DVR is busy serving another stream."), 503, "busy"),
    ],
)
def test_dvr_errors_map_to_friendly_http(error, status, fragment):
    with client_for(FakeClient(error=error)) as client:
        response = client.get(
            "/api/recordings",
            params={"channel": 101, "start": "2026-07-24T18:00:00Z", "end": "2026-07-24T19:00:00Z"},
        )
    assert response.status_code == status
    assert fragment in response.json()["detail"]


def test_expired_clip_id_returns_410():
    with client_for(FakeClient()) as client:
        assert client.get("/api/stream/doesnotexist").status_code == 410


def test_stream_failure_before_first_byte_is_a_real_error_status():
    """A dead DVR must produce 502, not a 200 with an empty body."""
    fake = FakeClient(stream_error=DvrUnreachable("Can't reach the DVR at 10.10.11.56."))
    with client_for(fake) as client:
        clips = client.get(
            "/api/recordings",
            params={"channel": 101, "start": "2026-07-24T18:00:00Z", "end": "2026-07-24T19:00:00Z"},
        ).json()["clips"]
        response = client.get(f"/api/stream/{clips[0]['id']}")
    assert response.status_code == 502
    assert "reach the DVR" in response.json()["detail"]


def test_raw_download_bypasses_remux_and_sets_filename():
    with client_for(FakeClient()) as client:
        clips = client.get(
            "/api/recordings",
            params={"channel": 101, "start": "2026-07-24T18:00:00Z", "end": "2026-07-24T19:00:00Z"},
        ).json()["clips"]
        response = client.get(f"/api/download/{clips[0]['id']}", params={"raw": 1})
    assert response.status_code == 200
    assert response.content == b"IMKH-fake-bytes"
    disposition = response.headers["content-disposition"]
    assert "attachment" in disposition
    assert "DriveWay1" in disposition and ".mpg" in disposition


def test_health_reports_dvr_state():
    with client_for(FakeClient()) as client:
        body = client.get("/api/health").json()
    assert body["dvr"] == "ok"
    assert body["clock_source"] == "configured"


def test_health_reports_client_remux_max_mb():
    with client_for(FakeClient(), client_remux_max_mb=200) as client:
        body = client.get("/api/health").json()
    assert body["client_remux_max_mb"] == 200


def test_recordings_are_ordered_newest_first():
    older = RECORDING
    newer = Recording(
        channel=101,
        start=datetime(2026, 7, 25, 9, 0, 0, tzinfo=timezone.utc),
        end=datetime(2026, 7, 25, 9, 1, 0, tzinfo=timezone.utc),
        size_bytes=1000,
        playback_uri="rtsp://10.10.11.56/Streaming/tracks/101/?starttime=20260725T090000Z&size=1000",
    )
    fake = FakeClient(recordings=[older, newer])
    with client_for(fake, max_results=40) as client:
        body = client.get(
            "/api/recordings",
            params={"channel": 101, "start": "2026-07-24T00:00:00Z", "end": "2026-07-26T00:00:00Z"},
        ).json()
    starts = [clip["start"] for clip in body["clips"]]
    assert starts == sorted(starts, reverse=True)
    assert starts[0].startswith("2026-07-25")


def test_index_is_served_at_root():
    with client_for(FakeClient()) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


# ── v0.4.0: snapshots, events, ingress middleware, ha_api ─────────────────────

def test_health_reports_ha_api_off_without_token():
    with client_for(FakeClient()) as client:
        body = client.get("/api/health").json()
    assert body["ha_api"] == "off"
    assert body["version"] == "0.6.0"


def test_recordings_include_events_list():
    with client_for(FakeClient()) as client:
        clip = client.get(
            "/api/recordings",
            params={"channel": 101, "start": "2026-07-24T18:00:00Z",
                    "end": "2026-07-24T19:00:00Z"},
        ).json()["clips"][0]
    assert clip["events"] == []


def test_snapshot_404_unknown_camera():
    with client_for(FakeClient()) as client:
        assert client.get("/api/snapshot/dvr-999").status_code == 404


def test_snapshot_404_when_not_linked():
    with client_for(FakeClient()) as client:
        response = client.get("/api/snapshot/dvr-101")
    assert response.status_code == 404
    assert "Not linked" in response.json()["detail"]


def test_snapshot_503_when_ha_disabled_but_linked():
    options = {
        **OPTIONS,
        "channels": [{"id": 101, "name": "Porch", "ha_camera": "camera.porch"}],
    }
    app = create_app(load_config(options), client=FakeClient())
    with TestClient(app) as client:
        response = client.get("/api/snapshot/dvr-101")
        channels = client.get("/api/channels").json()
    assert response.status_code == 503
    assert "homeassistant_api" in response.json()["detail"]
    assert channels[0]["snapshot"] is False


def test_snapshot_200_with_injected_ha_client():
    jpeg = b"\xff\xd8\xff\xd9fake"

    class FakeHa:
        enabled = True

        async def snapshot(self, entity_id: str):
            assert entity_id == "camera.porch"
            return jpeg, "image/jpeg"

        async def aclose(self):
            return None

    options = {
        **OPTIONS,
        "channels": [{"id": 101, "name": "Porch", "ha_camera": "camera.porch"}],
    }
    app = create_app(load_config(options), client=FakeClient(), ha_client=FakeHa())
    with TestClient(app) as client:
        response = client.get("/api/snapshot/dvr-101")
        channels = client.get("/api/channels").json()
        health = client.get("/api/health").json()
    assert response.status_code == 200
    assert response.content == jpeg
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert channels[0]["snapshot"] is True
    assert health["ha_api"] == "ok"


def test_ingress_middleware_off_without_token():
    """Local/dev: TestClient peer is not the ingress proxy, but middleware is off."""
    with client_for(FakeClient()) as client:
        assert client.get("/api/health").status_code == 200


@pytest.mark.asyncio
async def test_ingress_middleware_on_with_token(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token-not-real")
    app = create_app(load_config(OPTIONS), client=FakeClient())
    # Foreign peer → 403
    transport = httpx.ASGITransport(app=app, client=("192.0.2.99", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        forbidden = await client.get("/api/health")
    assert forbidden.status_code == 403
    # Ingress peer → allowed
    transport_ok = httpx.ASGITransport(app=app, client=("172.30.32.2", 12345))
    async with httpx.AsyncClient(transport=transport_ok, base_url="http://test") as client:
        ok = await client.get("/api/health")
    assert ok.status_code == 200
    assert ok.json()["ha_api"] == "ok"
    # Token was popped from the environment at create_app.
    assert "SUPERVISOR_TOKEN" not in __import__("os").environ
