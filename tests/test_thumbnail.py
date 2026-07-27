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
    ThumbnailError,
    split_jpegs,
    thumbnail_from_stream,
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


def _decode_all(payload: bytes) -> list[bytes]:
    """Every complete JPEG the real ffmpeg pipeline would emit for these bytes."""
    from hikvision_recordings.app.thumbnail import ffmpeg_thumb_args

    done = subprocess.run(ffmpeg_thumb_args(), input=payload, capture_output=True)
    return split_jpegs(done.stdout)


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
    """The whole point: a preview must not pull a 20-60 MB clip off the DVR."""
    padded = mpeg_ps_bytes + b"\0" * (THUMB_MAX_BYTES + 4 * 1024 * 1024)
    fake = FakeClient(padded, chunk=65536)
    with _client(fake) as client:
        response = client.get(f"/api/thumbnail/{_clip_id(client)}")
    assert response.status_code == 200
    assert fake.bytes_served <= THUMB_MAX_BYTES + fake.chunk, (
        f"read {fake.bytes_served} bytes; the cap is {THUMB_MAX_BYTES}"
    )
    assert fake.bytes_served < len(padded), "read the entire clip"


# ── Which frame the preview shows ────────────────────────────────────────────
# Frame 0 of a motion-triggered recording is usually the empty scene just before
# the event, so the preview is the LAST frame reachable inside the cheap read
# budget — "as late as the budget allows", not a fixed time target.
#
# The fixed t=15 s version was reverted: it needed ~13.5 MB and made every preview
# hold a DVR slot ~4x longer, which visibly stalled the list on a real phone.
# These tests exist so that does not come back by accident.

def test_budget_stays_cheap():
    """A regression guard on the revert itself, in bytes rather than prose."""
    assert THUMB_MAX_BYTES <= 2 * 1024 * 1024, (
        "the thumbnail read budget grew again — this is what stalled the list "
        "at ~13.5 MB and got the fixed 15 s seek reverted"
    )


def test_preview_is_a_later_frame_than_the_first_one(mpeg_ps_bytes):
    """The reason this feature exists: not frame 0.

    The fixture is 2 s of `testsrc`, whose picture changes every frame, so the last
    decodable frame must differ from the first. If these ever come out equal, the
    preview has silently regressed to frame 0.
    """
    frames = _decode_all(mpeg_ps_bytes)
    assert len(frames) > 1, "fixture yielded a single frame; test proves nothing"
    assert frames[-1] != frames[0], "preview is still the first frame"


def test_only_complete_frames_are_considered():
    """A truncated read can cut the final image in half; it must not be served."""
    good = b"\xff\xd8\xff" + b"a" * 20 + b"\xff\xd9"
    cut = b"\xff\xd8\xff" + b"b" * 10          # no EOI — chopped by the read cap
    assert split_jpegs(good + cut) == [good]
    assert split_jpegs(cut) == []


async def test_clip_shorter_than_the_budget_still_yields_a_real_thumbnail(tmp_path_factory):
    """A 1 s recording — real ones exist on this DVR — must still get a picture.

    Under the reverted design this was the risky case, because seeking 15 s into a
    1 s clip lands past the end. Nothing seeks now, so the whole clip simply fits
    inside the budget and its last frame is the preview.
    """
    out = tmp_path_factory.mktemp("short") / "short.ps"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=duration=1:size=640x360:rate=10",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "10", "-f", "mpeg", str(out)],
        check=True,
    )
    payload = out.read_bytes()
    assert len(payload) < THUMB_MAX_BYTES, "fixture is not actually shorter than the budget"

    async def source():
        for i in range(0, len(payload), 65536):
            yield payload[i : i + 65536]

    jpeg = await thumbnail_from_stream(source())
    assert jpeg.startswith(b"\xff\xd8\xff"), "a 1 s clip produced no thumbnail"


async def test_single_frame_clip_degrades_to_that_frame():
    """When only one frame decodes, first and last are the same — still a picture."""
    payload = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=duration=0.1:size=320x180:rate=10",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "mpeg", "pipe:1"],
        capture_output=True, check=True,
    ).stdout

    async def source():
        yield payload

    jpeg = await thumbnail_from_stream(source())
    assert jpeg.startswith(b"\xff\xd8\xff")
    assert jpeg.endswith(b"\xff\xd9")


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
