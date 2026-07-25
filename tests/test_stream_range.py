"""Regression tests for the 2026-07-25 live iOS playback failure.

THE BUG: /api/stream returned a chunked StreamingResponse with
`Accept-Ranges: none` and no Content-Length. iOS Safari / the HA Companion
WKWebView refuse to initialise <video> against such a response — Ramon got the
crossed-out "media unsupported" icon on a clip whose bytes were a perfectly valid
H.264 MP4 (ffprobe-confirmed; Download of the same clip worked fine).

THE FIX (pre-authorized in design spec section 7): stage the remuxed clip to a
RAM-backed temp file and serve it with FileResponse, which supplies
Content-Length + Accept-Ranges and answers 206 range requests.

These tests pin both halves of that: the response is genuinely seekable, AND the
staged file never outlives the response — because staging is the one place the
project's zero-persistence guarantee could be broken.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from hikvision_recordings.app import main as main_module
from hikvision_recordings.app.config import load_config
from hikvision_recordings.app.isapi import DvrUnreachable, Recording
from hikvision_recordings.app.main import create_app

# A tiny but real fragmented-MP4-shaped payload. Content does not matter to these
# tests — byte-exactness of the served slices does.
PAYLOAD = bytes(range(256)) * 200  # 51,200 bytes

RECORDING = Recording(
    channel=101,
    start=datetime(2026, 7, 25, 16, 48, 5, tzinfo=timezone.utc),
    end=datetime(2026, 7, 25, 16, 48, 42, tzinfo=timezone.utc),
    size_bytes=len(PAYLOAD),
    playback_uri="rtsp://10.10.11.56/Streaming/tracks/101/?starttime=20260725T114805Z&size=51200",
)

OPTIONS = {
    "dvr_host": "10.10.11.56",
    "dvr_username": "Jimmy",
    "dvr_password": "s3cret",
    "channels": [{"id": 101, "name": "DriveWay1"}],
}


class FakeClient:
    def __init__(self, payload: bytes = PAYLOAD, stream_error=None, chunk: int = 8192):
        self.payload = payload
        self.stream_error = stream_error
        self.chunk = chunk
        self.clock = SimpleNamespace(offset=timedelta(0), source="configured",
                                     drift=timedelta(0))

    async def probe_clock(self):
        return self.clock

    async def ping(self):
        return None

    async def search(self, channel, start_utc, end_utc, max_results=None):
        return [RECORDING]

    async def stream_download(self, playback_uri, chunk_size=262144):
        if self.stream_error:
            raise self.stream_error
        for i in range(0, len(self.payload), self.chunk):
            yield self.payload[i : i + self.chunk]

    async def aclose(self):
        return None


@pytest.fixture
def staging(tmp_path, monkeypatch) -> Path:
    """Point staging at a per-test dir so we can assert on exactly what it leaves."""
    monkeypatch.setattr(main_module, "STAGING_DIR", tmp_path)
    return tmp_path


def _client(fake: FakeClient, **overrides) -> TestClient:
    app = create_app(load_config({**OPTIONS, **overrides}), client=fake)
    return TestClient(app)


def _clip_id(client: TestClient) -> str:
    body = client.get(
        "/api/recordings",
        params={"channel": 101, "start": "2026-07-25T16:00:00Z", "end": "2026-07-25T17:00:00Z"},
    ).json()
    return body["clips"][0]["id"]


def _staged(staging: Path) -> list[Path]:
    return list(staging.glob(f"{main_module.STAGE_PREFIX}*"))


def test_stream_response_is_seekable(staging, monkeypatch):
    """The exact property iOS demanded and did not get: length + range support."""
    # remux is exercised elsewhere; here we care about the HTTP envelope.
    monkeypatch.setattr(main_module, "remux_to_fmp4", lambda src: src)
    with _client(FakeClient()) as client:
        response = client.get(f"/api/stream/{_clip_id(client)}")
    assert response.status_code == 200
    assert response.headers["content-length"] == str(len(PAYLOAD))
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers.get("accept-ranges") != "none", "the pre-fix value iOS rejected"
    assert response.content == PAYLOAD


def test_ranged_request_returns_206_with_the_right_slice(staging, monkeypatch):
    monkeypatch.setattr(main_module, "remux_to_fmp4", lambda src: src)
    with _client(FakeClient()) as client:
        clip = _clip_id(client)
        first = client.get(f"/api/stream/{clip}", headers={"Range": "bytes=0-1023"})
        middle = client.get(f"/api/stream/{clip}", headers={"Range": "bytes=2048-4095"})
    assert first.status_code == 206
    assert first.headers["content-range"] == f"bytes 0-1023/{len(PAYLOAD)}"
    assert first.content == PAYLOAD[0:1024]
    assert middle.status_code == 206
    assert middle.content == PAYLOAD[2048:4096]


def test_staged_file_is_deleted_after_the_response_completes(staging, monkeypatch):
    """Zero persistence: nothing may outlive the request that created it."""
    monkeypatch.setattr(main_module, "remux_to_fmp4", lambda src: src)
    with _client(FakeClient()) as client:
        response = client.get(f"/api/stream/{_clip_id(client)}")
        assert response.status_code == 200
    assert _staged(staging) == [], "a staged clip survived the response"


def test_staged_file_is_deleted_when_the_dvr_fails_midway(staging, monkeypatch):
    monkeypatch.setattr(main_module, "remux_to_fmp4", lambda src: src)
    fake = FakeClient(stream_error=DvrUnreachable("Can't reach the DVR at 10.10.11.56."))
    with _client(fake) as client:
        response = client.get(f"/api/stream/{_clip_id(client)}")
    assert response.status_code == 502
    assert _staged(staging) == [], "a failed stage left a file behind"


def test_oversize_clip_is_refused_rather_than_buffered_unbounded(staging, monkeypatch):
    """The cap must reject, not silently fill RAM."""
    monkeypatch.setattr(main_module, "remux_to_fmp4", lambda src: src)
    oversize = FakeClient(payload=b"\0" * (20 * 1024 * 1024))  # 20 MB against a 16 MB cap
    with _client(oversize, max_stage_mb=16) as client:
        response = client.get(f"/api/stream/{_clip_id(client)}")
    assert response.status_code == 413
    assert "Download" in response.json()["detail"]
    assert _staged(staging) == [], "the refused clip left a partial file behind"


def test_sweep_removes_an_orphan_from_a_crashed_response(staging, monkeypatch):
    """If a process died before its cleanup ran, the next stage must reap it."""
    monkeypatch.setattr(main_module, "remux_to_fmp4", lambda src: src)
    orphan = staging / f"{main_module.STAGE_PREFIX}orphan.mp4"
    orphan.write_bytes(b"stale")
    import os
    old = __import__("time").time() - (main_module.STAGE_MAX_AGE_S + 60)
    os.utime(orphan, (old, old))

    with _client(FakeClient()) as client:
        client.get(f"/api/stream/{_clip_id(client)}")
    assert not orphan.exists(), "an orphaned staged clip was not swept"
    assert _staged(staging) == []


def test_download_still_streams_without_staging(staging, monkeypatch):
    """Download works today as a plain browser download and needs no Range.

    Keeping it unstaged preserves the zero-buffer path for the large-file case;
    this test pins that decision so a later refactor does not silently stage it.
    """
    monkeypatch.setattr(main_module, "remux_to_fmp4", lambda src: src)
    with _client(FakeClient()) as client:
        response = client.get(f"/api/download/{_clip_id(client)}")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert response.content == PAYLOAD
    assert _staged(staging) == [], "download must not stage anything"
