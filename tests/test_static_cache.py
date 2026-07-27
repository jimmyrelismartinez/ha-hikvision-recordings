"""Static assets must state their freshness.

REGRESSION, 2026-07-27: Starlette's StaticFiles sends ETag + Last-Modified but no
Cache-Control. With no explicit freshness a browser applies heuristic caching and
may reuse an asset WITHOUT revalidating — iOS WKWebView (the HA Companion app)
especially. After updating v0.1.3 -> v0.1.4, Ramon's iPhone kept running the
CACHED v0.1.3 app.js: api/health reported 0.1.4 (a fresh API call) while the UI
was the old renderer, which had no SD/HD picker and no mode badge. The add-on
looked broken when it was simply never running the shipped frontend.

So the app's own HTML/JS/CSS must always revalidate, while the ~32 MB vendored
wasm core — pinned by version in the Dockerfile — should cache hard.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from hikvision_recordings.app.config import load_config
from hikvision_recordings.app.main import WWW_DIR, create_app

OPTIONS = {
    "dvr_host": "10.10.11.56",
    "dvr_username": "Jimmy",
    "dvr_password": "s3cret",
    "channels": [{"id": 101, "name": "DriveWay1"}],
}


class FakeClient:
    clock = SimpleNamespace(offset=timedelta(0), source="configured", drift=timedelta(0))

    async def probe_clock(self):
        return self.clock

    async def ping(self):
        return None

    async def aclose(self):
        return None


@pytest.fixture
def client():
    with TestClient(create_app(load_config(OPTIONS), client=FakeClient())) as c:
        yield c


@pytest.mark.parametrize("path", ["/", "/index.html", "/app.js", "/style.css"])
def test_app_assets_must_revalidate(client, path):
    """no-cache means 'revalidate before reuse' — not 'do not store'."""
    response = client.get(path)
    assert response.status_code == 200, path
    assert response.headers.get("cache-control") == "no-cache", (
        f"{path} has Cache-Control {response.headers.get('cache-control')!r}; without "
        "no-cache a browser may keep running a previous version's frontend"
    )


def test_revalidation_is_cheap(client):
    """The cost of no-cache is a 304, not a re-download of the file."""
    first = client.get("/app.js")
    etag = first.headers["etag"]
    again = client.get("/app.js", headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert not again.content


@pytest.mark.skipif(
    not (WWW_DIR / "vendor" / "ffmpeg.js").exists(),
    reason="ffmpeg.wasm not vendored locally (scripts/fetch-ffmpeg-wasm.sh)",
)
def test_vendored_wasm_is_cached_hard(client):
    """32 MB pinned by version in the Dockerfile — re-fetching it every load is waste."""
    response = client.get("/vendor/ffmpeg.js")
    assert response.status_code == 200
    cache_control = response.headers.get("cache-control", "")
    assert "immutable" in cache_control and "max-age=31536000" in cache_control, cache_control
