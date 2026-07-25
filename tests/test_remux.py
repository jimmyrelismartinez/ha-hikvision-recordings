import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from hikvision_recordings.app.remux import FFMPEG_ARGS, remux_to_fmp4

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@pytest.fixture(scope="module")
def mpeg_ps_bytes(tmp_path_factory) -> bytes:
    """Synthesise a small H.264-in-MPEG-PS file, the same shape the DVR returns."""
    out = tmp_path_factory.mktemp("fixtures") / "sample.ps"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "10",
            "-f", "mpeg", str(out),
        ],
        check=True,
    )
    return out.read_bytes()


def test_ffmpeg_args_are_stream_copy_and_use_the_verified_flags():
    assert "-c" in FFMPEG_ARGS and FFMPEG_ARGS[FFMPEG_ARGS.index("-c") + 1] == "copy"
    flags = FFMPEG_ARGS[FFMPEG_ARGS.index("-movflags") + 1]
    assert flags == "frag_keyframe+empty_moov"
    # Verified 2026-07-24: adding default_base_is_moof breaks ffmpeg 6.1 on this input.
    assert "default_base_is_moof" not in flags


async def test_remux_produces_a_progressively_playable_mp4(mpeg_ps_bytes, tmp_path):
    async def source():
        for i in range(0, len(mpeg_ps_bytes), 32768):
            yield mpeg_ps_bytes[i : i + 32768]

    chunks = [c async for c in remux_to_fmp4(source())]
    output = b"".join(chunks)

    assert output[4:8] == b"ftyp", "output must start with an ftyp box"
    assert b"moov" in output[:4096], "moov must be near the front (empty_moov) to stream"

    path = tmp_path / "out.mp4"
    path.write_bytes(output)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=format_name",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    assert "mp4" in probe.stdout


async def test_remux_emits_incrementally(mpeg_ps_bytes):
    """Output must arrive chunk by chunk, not as one blob at the end."""

    async def small_chunk_source():
        for i in range(0, len(mpeg_ps_bytes), 8192):
            yield mpeg_ps_bytes[i : i + 8192]

    stream = remux_to_fmp4(small_chunk_source())
    first = await stream.__anext__()
    assert first[4:8] == b"ftyp", "the very first chunk must already carry the MP4 header"
    async for _ in stream:
        pass


async def test_client_disconnect_midstream_does_not_raise(mpeg_ps_bytes):
    """Tapping a clip and immediately backing out must close cleanly, not error."""

    async def source():
        for i in range(0, len(mpeg_ps_bytes), 8192):
            yield mpeg_ps_bytes[i : i + 8192]

    stream = remux_to_fmp4(source())
    await stream.__anext__()
    await stream.aclose()  # must not raise


async def test_client_disconnect_before_any_output_does_not_raise():
    """Disconnecting before ffmpeg has produced a single byte must close cleanly.

    This is the only scenario that actually exercises the placement of the
    `produced == 0` guard relative to the try/finally in remux_to_fmp4: by the time
    any chunk has been yielded, produced > 0 and the guard is unreachable regardless
    of where it sits. `test_client_disconnect_midstream_does_not_raise` above always
    has produced > 0 by the time it closes (the first chunk is already ~787 bytes),
    so it cannot catch a regression here.
    """

    async def stalling_source():
        # Never send ffmpeg any bytes and never finish, so ffmpeg's stdout.read()
        # blocks and `produced` stays 0 for as long as this test needs it to.
        await asyncio.sleep(3600)
        yield b""  # pragma: no cover - unreachable within the test's lifetime

    stream = remux_to_fmp4(stalling_source())
    task = asyncio.ensure_future(stream.__anext__())
    await asyncio.sleep(0.3)  # let ffmpeg spawn and block on stdout.read()
    assert not task.done()  # confirms we're disconnecting before any output exists
    task.cancel()

    # A clean disconnect here must surface as CancelledError, not RemuxError. If the
    # `produced == 0` check were inside the finally block, it would fire while
    # CancelledError is propagating and replace it with RemuxError.
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    # The generator is fully unwound now; a following aclose() must be a no-op.
    await asyncio.wait_for(stream.aclose(), timeout=5)


async def test_garbage_input_raises_remux_error():
    from hikvision_recordings.app.remux import RemuxError

    async def source():
        yield b"this is not video at all" * 100

    with pytest.raises(RemuxError):
        async for _ in remux_to_fmp4(source()):
            pass


async def test_source_error_before_first_byte_propagates_its_own_type():
    """A source that dies before producing any bytes must surface its own exception.

    Regression: the feeder task's exception used to be swallowed by the cleanup
    `finally` (`await feeder` under `except (asyncio.CancelledError, Exception): pass`),
    so a DVR-side failure (e.g. DvrUnreachable) came out of remux_to_fmp4 as a generic
    RemuxError — hiding the real cause and mapping to the wrong HTTP status upstream
    (a 500 instead of a 502/401/503). Found while wiring app/main.py (Task 8).
    """

    class SourceBoom(Exception):
        pass

    async def dying_source():
        raise SourceBoom("DVR is unreachable")
        yield b""  # pragma: no cover - unreachable, keeps this an async generator

    with pytest.raises(SourceBoom):
        async for _ in remux_to_fmp4(dying_source()):
            pass
