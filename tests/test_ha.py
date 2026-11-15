"""Home Assistant snapshot client — MockTransport only, never a real network."""
from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from hikvision_recordings.app.ha import (
    HaAuthError,
    HaBadEntity,
    HaClient,
    HaNotFound,
    HaUnreachable,
)


TOKEN = "test-supervisor-token-not-real"
JPEG = b"\xff\xd8\xff\xd9" + b"fake-jpeg"
PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png"


def _client(handler, **kwargs) -> HaClient:
    transport = httpx.MockTransport(handler)
    return HaClient(TOKEN, transport=transport, base_url="http://supervisor/core/api", **kwargs)


@pytest.mark.asyncio
async def test_disabled_without_token():
    ha = HaClient(None)
    assert ha.enabled is False
    await ha.aclose()


@pytest.mark.asyncio
async def test_sends_bearer_token_header():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})

    ha = _client(handler)
    body, ctype = await ha.snapshot("camera.porch")
    await ha.aclose()
    assert body == JPEG and ctype == "image/jpeg"
    assert seen["auth"] == f"Bearer {TOKEN}"
    assert seen["url"].endswith("/camera_proxy/camera.porch")
    assert TOKEN not in body.decode("latin1", "replace")


@pytest.mark.parametrize("bad", [
    "camera.x\n",
    "camera.x/../states",
    "light.x",
    "Camera.porch",
    "camera.UPPER",
])
@pytest.mark.asyncio
async def test_rejects_bad_entity_ids(bad):
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not call upstream")

    ha = _client(handler)
    with pytest.raises(HaBadEntity):
        await ha.snapshot(bad)
    await ha.aclose()


@pytest.mark.asyncio
async def test_single_flight_shares_one_upstream_fetch():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})

    ha = _client(handler, ttl_s=2.0)
    a, b = await asyncio.gather(ha.snapshot("camera.porch"), ha.snapshot("camera.porch"))
    await ha.aclose()
    assert a == b == (JPEG, "image/jpeg")
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_ttl_cache_hit_then_expiry(monkeypatch):
    calls = {"n": 0}
    now = {"t": 1000.0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})

    monkeypatch.setattr(
        "hikvision_recordings.app.ha.time.monotonic", lambda: now["t"]
    )
    ha = _client(handler, ttl_s=2.0)
    await ha.snapshot("camera.driveway")
    now["t"] += 1.0
    await ha.snapshot("camera.driveway")
    assert calls["n"] == 1
    now["t"] += 2.0
    await ha.snapshot("camera.driveway")
    assert calls["n"] == 2
    await ha.aclose()


@pytest.mark.asyncio
async def test_failures_are_cached_for_ttl(monkeypatch):
    calls = {"n": 0}
    now = {"t": 1000.0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, content=b"nope")

    monkeypatch.setattr(
        "hikvision_recordings.app.ha.time.monotonic", lambda: now["t"]
    )
    ha = _client(handler, ttl_s=2.0)
    with pytest.raises(HaUnreachable):
        await ha.snapshot("camera.garden")
    with pytest.raises(HaUnreachable):
        await ha.snapshot("camera.garden")
    assert calls["n"] == 1
    now["t"] += 3.0
    with pytest.raises(HaUnreachable):
        await ha.snapshot("camera.garden")
    assert calls["n"] == 2
    await ha.aclose()


@pytest.mark.asyncio
async def test_non_image_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b'{"state":"idle"}', headers={"content-type": "application/json"}
        )

    ha = _client(handler)
    with pytest.raises(HaUnreachable):
        await ha.snapshot("camera.porch")
    await ha.aclose()


@pytest.mark.asyncio
async def test_size_cap_stops_stream():
    big = b"\xff\xd8\xff" + b"x" * (6 * 1024 * 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=big, headers={"content-type": "image/jpeg"})

    ha = _client(handler, max_bytes=5 * 1024 * 1024)
    with pytest.raises(HaUnreachable):
        await ha.snapshot("camera.porch")
    await ha.aclose()


@pytest.mark.asyncio
async def test_status_mapping_404_401_5xx(caplog):
    responses = {
        "camera.missing": httpx.Response(404, content=b"gone"),
        "camera.denied": httpx.Response(401, content=b"no"),
        "camera.broken": httpx.Response(503, content=b"busy"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        entity = request.url.path.rsplit("/", 1)[-1]
        return responses[entity]

    ha = _client(handler)
    with pytest.raises(HaNotFound, match="camera.missing"):
        await ha.snapshot("camera.missing")
    with caplog.at_level(logging.ERROR):
        with pytest.raises(HaAuthError, match="did not return a snapshot"):
            await ha.snapshot("camera.denied")
    assert "homeassistant_api" in caplog.text
    assert TOKEN not in caplog.text
    with pytest.raises(HaUnreachable, match="did not return a snapshot"):
        await ha.snapshot("camera.broken")
    await ha.aclose()


@pytest.mark.asyncio
async def test_png_accepted():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    ha = _client(handler)
    body, ctype = await ha.snapshot("camera.porch")
    await ha.aclose()
    assert body == PNG and ctype == "image/png"


@pytest.mark.asyncio
async def test_token_never_in_error_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, content=b"upstream")

    ha = _client(handler)
    with pytest.raises(HaUnreachable) as exc:
        await ha.snapshot("camera.porch")
    await ha.aclose()
    assert TOKEN not in str(exc.value)
