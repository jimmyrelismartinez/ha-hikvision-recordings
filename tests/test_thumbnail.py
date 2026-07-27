"""Tests for per-clip thumbnails.

The endpoint reads only the first slice of a clip and decodes one frame with
ffmpeg. Two properties matter beyond "it returns an image":

  * It must go through IsapiClient.stream_download, so thumbnails contend for the
    same max_concurrent_downloads budget as playback/download instead of opening
    an unlimited side channel — a 40-row list would otherwise swamp the DVR.
  * An undecodable clip must degrade to a clean error (one placeholder row), never
    a hang or a 500.
"""
from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from hikvision_recordings.app.config import load_config
from hikvision_recordings.app.isapi import DvrBusy, DvrUnreachable, Recording
from hikvision_recordings.app.main import create_app
from hikvision_recordings.app.thumbnail import (
    THUMB_MAX_BYTES,
    THUMB_TARGET_S,
    ThumbnailError,
    _max_bytes_for,
    thumbnail_from_stream,
    thumbnail_offset_for,
)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

RECORDING = Recording(
    channel=101,
    start=datetime(2026, 7, 25, 16, 48, 5, tzinfo=timezone.utc),
    end=datetime(2026, 7, 25, 16, 48, 42, tzinfo=timezone.utc),
    size_bytes=19_761_375,
    playback_uri="rtsp://10.10.11.56/Streaming/tracks/101/?starttime=20260725T114805Z&size=19761375",
)

OPTIONS = {
    "dvr_host": "10.10.11.56",
    "dvr_username": "Jimmy",
    "dvr_password": "s3cret",
    "channels": [{"id": 101, "name": "DriveWay1"}],
}


@pytest.fixture(scope="module")
def mpeg_ps_bytes(tmp_path_factory) -> bytes:
    """Synthesise H.264-in-MPEG-PS, the same container the DVR download returns."""
    out = tmp_path_factory.mktemp("thumb") / "sample.ps"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=duration=2:size=640x360:rate=10",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "10", "-f", "mpeg", str(out)],
        check=True,
    )
    return out.read_bytes()


class FakeClient:
    def __init__(self, payload: bytes, stream_error=None, chunk: int = 65536):
        self.payload = payload
        self.stream_error = stream_error
        self.chunk = chunk
        self.stream_calls = 0
        self.bytes_served = 0
        self.clock = SimpleNamespace(offset=timedelta(0), source="configured",
                                     drift=timedelta(0))

    async def probe_clock(self):
        return self.clock

    async def ping(self):
        return None

    async def search(self, channel, start_utc, end_utc, max_results=None):
        return [RECORDING]

    async def stream_download(self, playback_uri, chunk_size=262144):
        self.stream_calls += 1
        if self.stream_error:
            raise self.stream_error
        for i in range(0, len(self.payload), self.chunk):
            block = self.payload[i : i + self.chunk]
            self.bytes_served += len(block)
            yield block

    async def aclose(self):
        return None


def _client(fake: FakeClient, **overrides) -> TestClient:
    return TestClient(create_app(load_config({**OPTIONS, **overrides}), client=fake))


def _clip_id(client: TestClient) -> str:
    body = client.get(
        "/api/recordings",
        params={"channel": 101, "start": "2026-07-25T16:00:00Z", "end": "2026-07-25T17:00:00Z"},
    ).json()
    return body["clips"][0]["id"]


def test_returns_a_real_decodable_jpeg(mpeg_ps_bytes, tmp_path):
    fake = FakeClient(mpeg_ps_bytes)
    with _client(fake) as client:
        response = client.get(f"/api/thumbnail/{_clip_id(client)}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content.startswith(b"\xff\xd8\xff"), "not a JPEG"

    # Prove it decodes as an image rather than merely starting with the magic.
    path = tmp_path / "t.jpg"
    path.write_bytes(response.content)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v",
         "-show_entries", "stream=codec_name,width,height", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    codec, width, height = probe.stdout.strip().split(",")
    assert codec == "mjpeg"
    assert int(width) == 480, "should be scaled down for the list, not full-res"
    assert int(height) > 0


def test_reads_only_a_bounded_slice_not_the_whole_clip(mpeg_ps_bytes):
    """A preview must not pull a 20-60 MB clip off the DVR.

    The budget is no longer a flat 1 MB. Since the preview frame comes from ~15 s
    in rather than frame 0, reaching it needs proportionally more bytes — see the
    bitrate reasoning in thumbnail.py. What must still hold is that the read is
    BOUNDED by that budget and stops well short of a whole clip.
    """
    budget = _max_bytes_for(thumbnail_offset_for(RECORDING.duration_s))
    padded = mpeg_ps_bytes + b"\0" * (budget + 8 * 1024 * 1024)
    fake = FakeClient(padded, chunk=65536)
    with _client(fake) as client:
        response = client.get(f"/api/thumbnail/{_clip_id(client)}")
    assert response.status_code == 200
    assert fake.bytes_served <= budget + fake.chunk, (
        f"read {fake.bytes_served} bytes; the budget is {budget}"
    )
    assert fake.bytes_served < len(padded), "read the entire clip"


# ── Preview frame timing (v0.1.4) ────────────────────────────────────────────
# Frame 0 of a motion-triggered recording is usually the empty scene just before
# the event, so the preview is taken ~15 s in. Clips as short as 1 s exist on this
# DVR, so the clamp and the fallback are what keep that from becoming a new
# failure mode.

def test_long_clip_seeks_to_the_target_time():
    assert thumbnail_offset_for(60) == THUMB_TARGET_S
    assert thumbnail_offset_for(15) == THUMB_TARGET_S


@pytest.mark.parametrize("duration_s", [3, 5, 10, 14])
def test_short_clip_is_clamped_to_inside_the_clip(duration_s):
    offset = thumbnail_offset_for(duration_s)
    assert 0 < offset < duration_s, "seek target must land inside the clip"


@pytest.mark.parametrize("duration_s", [0, 1, 2, None])
def test_very_short_or_unknown_duration_falls_back_to_frame_zero(duration_s):
    """Seeking fractions of a second buys nothing and risks passing the only keyframe."""
    assert thumbnail_offset_for(duration_s) == 0.0


def test_budget_scales_with_the_seek_target_but_never_below_the_floor():
    assert _max_bytes_for(0.0) == THUMB_MAX_BYTES
    assert _max_bytes_for(THUMB_TARGET_S) > _max_bytes_for(1.0) > THUMB_MAX_BYTES - 1


async def test_one_second_clip_still_yields_a_real_thumbnail(tmp_path_factory):
    """A 1 s recording — real ones exist on this DVR — must still get a picture.

    This is the regression Ramon called out: seeking 15 s into a 1 s clip must not
    turn a working thumbnail into an error.
    """
    out = tmp_path_factory.mktemp("short") / "short.ps"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=duration=1:size=640x360:rate=10",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "10", "-f", "mpeg", str(out)],
        check=True,
    )
    payload = out.read_bytes()

    async def source():
        for i in range(0, len(payload), 65536):
            yield payload[i : i + 65536]

    jpeg = await thumbnail_from_stream(source(), duration_s=1)
    assert jpeg.startswith(b"\xff\xd8\xff"), "a 1 s clip produced no thumbnail"


async def test_unreachable_seek_target_retries_at_frame_zero(mpeg_ps_bytes):
    """If the seek finds no frame, the SAME bytes are retried at offset 0.

    The fixture clip is 2 s long but is described here as 60 s, so the 15 s seek
    target is past its end — exactly the mismatch a wrong-duration search result
    would produce. It must still return a picture, without a second DVR fetch.
    """
    reads = {"n": 0}

    async def source():
        reads["n"] += 1
        for i in range(0, len(mpeg_ps_bytes), 65536):
            yield mpeg_ps_bytes[i : i + 65536]

    jpeg = await thumbnail_from_stream(source(), duration_s=60)
    assert jpeg.startswith(b"\xff\xd8\xff"), "no fallback frame was produced"
    assert reads["n"] == 1, "retry re-fetched from the DVR instead of reusing the bytes"


def test_uses_the_shared_dvr_connection_budget(mpeg_ps_bytes):
    """Thumbnails must go through stream_download, which holds the semaphore."""
    fake = FakeClient(mpeg_ps_bytes)
    with _client(fake) as client:
        client.get(f"/api/thumbnail/{_clip_id(client)}")
    assert fake.stream_calls == 1, "endpoint bypassed stream_download's concurrency guard"


def test_busy_dvr_surfaces_as_503_like_the_video_endpoints(mpeg_ps_bytes):
    fake = FakeClient(mpeg_ps_bytes, stream_error=DvrBusy("DVR is busy serving another stream."))
    with _client(fake) as client:
        response = client.get(f"/api/thumbnail/{_clip_id(client)}")
    assert response.status_code == 503
    assert "busy" in response.json()["detail"].lower()


def test_unreachable_dvr_surfaces_as_502(mpeg_ps_bytes):
    fake = FakeClient(mpeg_ps_bytes, stream_error=DvrUnreachable("Can't reach the DVR."))
    with _client(fake) as client:
        response = client.get(f"/api/thumbnail/{_clip_id(client)}")
    assert response.status_code == 502


def test_undecodable_clip_is_a_clean_error_not_a_crash(mpeg_ps_bytes):
    """A corrupt clip shows a placeholder on one row; it must not 500 or hang."""
    fake = FakeClient(b"this is not video at all" * 5000)
    with _client(fake) as client:
        response = client.get(f"/api/thumbnail/{_clip_id(client)}")
    assert response.status_code == 502
    assert "preview" in response.json()["detail"].lower()


def test_expired_clip_id_returns_410_like_the_other_endpoints():
    fake = FakeClient(b"")
    with _client(fake) as client:
        assert client.get("/api/thumbnail/doesnotexist").status_code == 410


async def test_empty_source_raises_thumbnail_error():
    async def empty():
        return
        yield b""  # pragma: no cover

    with pytest.raises(ThumbnailError):
        await thumbnail_from_stream(empty())


async def test_source_is_closed_so_the_semaphore_permit_is_returned(mpeg_ps_bytes):
    """Abandoning the download early must release the DVR permit, not leak it."""
    closed = {"yes": False}

    async def source():
        try:
            for i in range(0, len(mpeg_ps_bytes) + 4_000_000, 65536):
                yield (mpeg_ps_bytes + b"\0" * 4_000_000)[i : i + 65536]
        finally:
            closed["yes"] = True

    jpeg = await thumbnail_from_stream(source())
    assert jpeg.startswith(b"\xff\xd8\xff")
    assert closed["yes"], "the DVR source generator was never closed"
