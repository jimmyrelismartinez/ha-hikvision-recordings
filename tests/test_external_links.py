"""External playback links (v0.7 / issue #5) — mint, link app, cache, middleware."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.testclient import TestClient as StarletteTestClient

from hikvision_recordings.app import main as main_module
from hikvision_recordings.app.config import load_config
from hikvision_recordings.app.isapi import Recording
from hikvision_recordings.app.main import IngressOnlyMiddleware, create_apps
from hikvision_recordings.app.token import mint as mint_token
from hikvision_recordings.app.token import verify as verify_token

MINT_TOKEN = "a" * 24
BASE_URL = "http://192.0.2.10:8100"

OPTIONS = {
    "dvr_host": "192.0.2.50",
    "dvr_username": "viewer",
    "dvr_password": "s3cret",
    "channels": [{"id": 101, "name": "Driveway"}],
    "external_links": True,
    "external_mint_token": MINT_TOKEN,
    "external_link_ttl_s": 300,
    "external_base_url": BASE_URL,
}

PAYLOAD = bytes(range(256)) * 40  # 10,240 bytes

NOW = datetime(2026, 11, 17, 12, 0, 0, tzinfo=timezone.utc)

RECORDING = Recording(
    channel=101,
    start=NOW - timedelta(minutes=5),
    end=NOW - timedelta(minutes=4),
    size_bytes=len(PAYLOAD),
    playback_uri=(
        "rtsp://192.0.2.50/Streaming/tracks/101/"
        "?starttime=20261117T115500Z&size=10240"
    ),
    events=("motion",),
)


class FakeClient:
    def __init__(self, recordings=None, payload: bytes = PAYLOAD):
        self.recordings = list(recordings) if recordings is not None else [RECORDING]
        self.payload = payload
        self.stream_calls = 0
        self.calls = []
        self.clock = SimpleNamespace(
            offset=timedelta(0), source="configured", drift=timedelta(0),
        )

    async def probe_clock(self):
        return self.clock

    async def ping(self):
        return None

    async def search(self, channel, start_utc, end_utc, max_results=None):
        self.calls.append((channel, start_utc, end_utc, max_results))
        return [r for r in self.recordings if r.channel == channel or channel == 101]

    async def stream_download(self, playback_uri, chunk_size=262144):
        self.stream_calls += 1
        for i in range(0, len(self.payload), 4096):
            yield self.payload[i : i + 4096]

    async def aclose(self):
        return None


def _cfg(**overrides):
    return load_config({**OPTIONS, **overrides})


def _apps(fake=None, **overrides):
    fake = fake or FakeClient()
    return create_apps(_cfg(**overrides), client=fake), fake


def _auth():
    return {"Authorization": f"Bearer {MINT_TOKEN}"}


@pytest.fixture
def staging(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "STAGING_DIR", tmp_path)
    monkeypatch.setattr(main_module, "remux_to_fmp4", lambda src: src)
    return tmp_path


def _freeze_clock(app, when: datetime):
    app.state.clock.freeze(when)


# ── config ───────────────────────────────────────────────────────────────────

def test_link_options_invalid_do_not_raise(caplog):
    with caplog.at_level(logging.ERROR):
        cfg = load_config({
            **{k: v for k, v in OPTIONS.items() if k != "external_mint_token"},
            "external_links": True,
            "external_mint_token": "short",
            "external_base_url": "not-a-url",
        })
    assert cfg.external_links is True
    assert cfg.external_links_ok is False
    assert any("external links misconfigured" in r.message for r in caplog.records)


def test_base_url_trailing_slash_stripped():
    cfg = load_config({**OPTIONS, "external_base_url": "http://192.0.2.10:8100/"})
    assert cfg.external_base_url == "http://192.0.2.10:8100"
    assert cfg.external_links_ok


def test_ttl_cap_in_schema_defaults():
    cfg = load_config({**OPTIONS, "external_link_ttl_s": 900})
    assert cfg.external_link_ttl_s == 900


# ── mint auth + middleware ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mint_bearer_required_even_from_ingress(monkeypatch, staging):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token-not-real")
    (ingress, link), fake = _apps()
    assert link is not None
    transport = httpx.ASGITransport(app=ingress, client=("172.30.32.2", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        no_auth = await client.post("/internal/mint", json={"camera": "dvr-101"})
        bad = await client.post(
            "/internal/mint",
            json={"camera": "dvr-101"},
            headers={"Authorization": "Bearer wrong-token-wrong-token!!"},
        )
        _freeze_clock(ingress, NOW)
        ok = await client.post(
            "/internal/mint",
            json={"camera": "dvr-101"},
            headers=_auth(),
        )
    assert no_auth.status_code == 401
    assert bad.status_code == 401
    assert ok.status_code == 200
    assert ok.json()["content_type"] == "video/mp4"
    assert ok.json()["url"].startswith(BASE_URL + "/m/")
    assert ok.json()["url"].endswith("/clip.mp4")


@pytest.mark.asyncio
async def test_middleware_exemption_exactness(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token-not-real")
    (ingress, _), _ = _apps()
    foreign = httpx.ASGITransport(app=ingress, client=("192.0.2.99", 1))
    async with httpx.AsyncClient(transport=foreign, base_url="http://test") as client:
        # Exact path+method exempt (still needs bearer → 401, not 403).
        mint = await client.post("/internal/mint", json={"camera": "dvr-101"})
        slash = await client.post("/internal/mint/", json={"camera": "dvr-101"})
        get_mint = await client.get("/internal/mint")
        health = await client.get("/api/health")
    assert mint.status_code == 401
    assert slash.status_code == 403
    assert get_mint.status_code == 403
    assert health.status_code == 403


@pytest.mark.asyncio
async def test_mint_off_is_404_misconfigured_is_503(monkeypatch, staging):
    (off, link_off), _ = _apps(external_links=False)
    assert link_off is None
    with TestClient(off) as client:
        assert client.post(
            "/internal/mint", json={"camera": "dvr-101"}, headers=_auth(),
        ).status_code == 404

    bad = load_config({
        **OPTIONS,
        "external_mint_token": "x",
        "external_base_url": "ftp://nope",
    })
    assert bad.external_links_ok is False
    from hikvision_recordings.app.main import create_apps as ca
    ingress, link = ca(bad, client=FakeClient())
    assert link is None
    with TestClient(ingress) as client:
        # Bearer may match the short token; misconfigured → 503.
        r = client.post(
            "/internal/mint",
            json={"camera": "dvr-101"},
            headers={"Authorization": "Bearer x"},
        )
    assert r.status_code == 503
    assert r.json()["detail"] == "disabled_misconfigured"


def test_unsupported_event_400(staging):
    (ingress, _), _ = _apps()
    _freeze_clock(ingress, NOW)
    with TestClient(ingress) as client:
        r = client.post(
            "/internal/mint",
            json={"camera": "dvr-101", "event": "person"},
            headers=_auth(),
        )
    assert r.status_code == 400
    assert r.json()["detail"] == "unsupported_event"


def test_open_clip_rejected_with_fake_clock(staging):
    """end within 3 s of now is not closed."""
    open_rec = Recording(
        channel=101,
        start=NOW - timedelta(seconds=30),
        end=NOW - timedelta(seconds=1),
        size_bytes=100,
        playback_uri=RECORDING.playback_uri + "&open=1",
        events=("motion",),
    )
    (ingress, _), _ = _apps(FakeClient(recordings=[open_rec]))
    _freeze_clock(ingress, NOW)
    with TestClient(ingress) as client:
        r = client.post(
            "/internal/mint",
            json={"camera": "dvr-101"},
            headers=_auth(),
        )
    assert r.status_code == 404
    assert r.json()["detail"] == "not_ready"


# ── link app ─────────────────────────────────────────────────────────────────

def test_link_app_only_clip_route(staging):
    (ingress, link), _ = _apps()
    assert link is not None
    with StarletteTestClient(link) as client:
        for path in ("/", "/docs", "/openapi.json", "/api/health", "/m/x"):
            assert client.get(path).status_code == 404


def test_link_range_206_and_head(staging):
    (ingress, link), fake = _apps()
    _freeze_clock(ingress, NOW)
    with TestClient(ingress) as client:
        body = client.post(
            "/internal/mint", json={"camera": "dvr-101"}, headers=_auth(),
        ).json()
    token = body["url"].split("/m/")[1].split("/")[0]
    with StarletteTestClient(link) as client:
        head = client.head(f"/m/{token}/clip.mp4")
        full = client.get(f"/m/{token}/clip.mp4")
        ranged = client.get(
            f"/m/{token}/clip.mp4", headers={"Range": "bytes=0-99"},
        )
    assert head.status_code == 200
    assert full.status_code == 200
    assert full.content == PAYLOAD
    assert ranged.status_code == 206
    assert ranged.content == PAYLOAD[:100]
    assert fake.stream_calls == 1


def test_expired_token_403_while_stage_cached(staging):
    (ingress, link), _ = _apps()
    _freeze_clock(ingress, NOW)
    with TestClient(ingress) as client:
        body = client.post(
            "/internal/mint", json={"camera": "dvr-101"}, headers=_auth(),
        ).json()
    token = body["url"].split("/m/")[1].split("/")[0]
    # Advance past expiry but keep the stage entry alive (hard cap 900 s).
    later = NOW + timedelta(seconds=400)
    _freeze_clock(ingress, later)
    with StarletteTestClient(link) as client:
        r = client.get(f"/m/{token}/clip.mp4")
    assert r.status_code == 403


def test_evicted_entry_410(staging):
    (ingress, link), _ = _apps()
    cache = ingress.state.link_cache
    _freeze_clock(ingress, NOW)
    with TestClient(ingress) as client:
        body = client.post(
            "/internal/mint", json={"camera": "dvr-101"}, headers=_auth(),
        ).json()
    token = body["url"].split("/m/")[1].split("/")[0]
    payload = verify_token(cache.signing_key, token)
    cache._evict(payload["e"])
    with StarletteTestClient(link) as client:
        assert client.get(f"/m/{token}/clip.mp4").status_code == 410


@pytest.mark.asyncio
async def test_single_flight_one_download_for_concurrent_mints(staging):
    (ingress, _), fake = _apps()
    _freeze_clock(ingress, NOW)
    transport = httpx.ASGITransport(app=ingress)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        async def one():
            return await client.post(
                "/internal/mint",
                json={"camera": "dvr-101"},
                headers=_auth(),
            )
        a, b = await asyncio.gather(one(), one())
    assert a.status_code == 200 and b.status_code == 200
    assert fake.stream_calls == 1
    pa = verify_token(
        ingress.state.link_cache.signing_key,
        a.json()["url"].split("/m/")[1].split("/")[0],
    )
    pb = verify_token(
        ingress.state.link_cache.signing_key,
        b.json()["url"].split("/m/")[1].split("/")[0],
    )
    assert pa["e"] == pb["e"]


def test_mint_bearer_rejects_short_token_without_500(staging):
    (ingress, _), _ = _apps()
    with TestClient(ingress) as client:
        r = client.post(
            "/internal/mint",
            json={"camera": "dvr-101"},
            headers={"Authorization": "Bearer short"},
        )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_since_wait_polls_until_closed(staging, monkeypatch):
    """Growing end on poll 1 → stable closed end on polls 2 and 3 within wait_s."""
    start = NOW - timedelta(seconds=20)
    uri = "rtsp://192.0.2.50/Streaming/tracks/101/?starttime=x&size=100"
    holder: dict = {}

    class PollingFake(FakeClient):
        def __init__(self):
            super().__init__(recordings=[])
            self.polls = 0

        async def search(self, channel, start_utc, end_utc, max_results=None):
            self.polls += 1
            app = holder["app"]
            # Poll 1: still "open" (end too close to now). Later polls: stable closed end.
            if self.polls == 1:
                end = NOW - timedelta(seconds=1)
            else:
                app.state.clock.freeze(NOW + timedelta(seconds=5 * (self.polls - 1)))
                end = NOW - timedelta(seconds=10)
            rec = Recording(
                channel=101, start=start, end=end, size_bytes=100,
                playback_uri=uri, events=("motion",),
            )
            self.recordings = [rec]
            return [rec]

    fake = PollingFake()
    (ingress, _), _ = _apps(fake)
    holder["app"] = ingress
    _freeze_clock(ingress, NOW)
    real_sleep = asyncio.sleep

    async def fast_sleep(_):
        await real_sleep(0)

    monkeypatch.setattr(main_module.asyncio, "sleep", fast_sleep)

    transport = httpx.ASGITransport(app=ingress)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.post(
            "/internal/mint",
            json={
                "camera": "dvr-101",
                "since": start.isoformat().replace("+00:00", "Z"),
                "wait_s": 30,
                "event": "motion",
            },
            headers=_auth(),
        )
    assert r.status_code == 200, r.text
    assert fake.polls >= 3


def test_eviction_keeps_at_most_two(staging):
    recs = []
    for i in range(3):
        start = NOW - timedelta(minutes=10 + i)
        end = start + timedelta(seconds=30)
        recs.append(Recording(
            channel=101,
            start=start,
            end=end,
            size_bytes=100,
            playback_uri=f"rtsp://192.0.2.50/Streaming/tracks/101/?n={i}&size=100",
            events=("motion",),
        ))
    fake = FakeClient(recordings=recs)
    (ingress, _), _ = _apps(fake)
    _freeze_clock(ingress, NOW)
    cache = ingress.state.link_cache
    with TestClient(ingress) as client:
        # Mint without since returns newest; force distinct clips via since windows.
        for rec in recs:
            r = client.post(
                "/internal/mint",
                json={
                    "camera": "dvr-101",
                    "since": rec.start.isoformat().replace("+00:00", "Z"),
                },
                headers=_auth(),
            )
            assert r.status_code == 200, r.text
    assert len(cache._by_id) <= 2


def test_sweep_prefix_isolation(staging):
    """UI hikclip- sweep must not touch hiklink- files."""
    ui = staging / "hikclip-keep.mp4"
    link = staging / "hiklink-keep.mp4"
    ui.write_bytes(b"ui")
    link.write_bytes(b"link")
    old = main_module.time.time() - main_module.STAGE_MAX_AGE_S - 10
    os.utime(ui, (old, old))
    os.utime(link, (old, old))
    main_module._sweep_stale_stages()
    assert not ui.exists()
    assert link.exists()


def test_token_absent_from_logs_at_trace(staging, caplog):
    """Access log is off; our loggers at TRACE must never print the token path."""
    (ingress, link), _ = _apps()
    _freeze_clock(ingress, NOW)
    with TestClient(ingress) as client:
        body = client.post(
            "/internal/mint", json={"camera": "dvr-101"}, headers=_auth(),
        ).json()
    token = body["url"].split("/m/")[1].split("/")[0]
    # Only assert on app / uvicorn loggers — httpx TestClient logs URLs itself.
    watched = (
        "hikvision_recordings",
        "hikvision_recordings.app",
        "hikvision_recordings.app.main",
        "hikvision_recordings.app.serve",
        "uvicorn",
        "uvicorn.access",
        "uvicorn.error",
    )
    with caplog.at_level(logging.DEBUG):
        for name in watched:
            logging.getLogger(name).setLevel(logging.DEBUG)
        with StarletteTestClient(link) as client:
            client.get(f"/m/{token}/clip.mp4")
    joined = " ".join(
        r.getMessage() for r in caplog.records
        if r.name.startswith(watched) or r.name in watched
    )
    assert token not in joined
    assert MINT_TOKEN not in joined


@pytest.mark.asyncio
async def test_launcher_keeps_ingress_middleware_enabled(monkeypatch, staging):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "launcher-token-not-real")
    monkeypatch.setenv("LOG_LEVEL", "info")
    # Build the same way serve.py does (without binding ports).
    from hikvision_recordings.app.main import create_apps as ca
    ingress, link = ca(_cfg(), client=FakeClient())
    assert "SUPERVISOR_TOKEN" not in os.environ
    # Find IngressOnlyMiddleware
    mw = None
    app = ingress
    while hasattr(app, "app"):
        if isinstance(app, IngressOnlyMiddleware) or type(app).__name__ == "IngressOnlyMiddleware":
            mw = app
            break
        # Starlette wraps middleware; walk user_middleware
        break
    # FastAPI stores middleware in user_middleware; the enabled flag is on the class instance
    # created at request time. Inspect via a foreign-peer call instead.
    transport = httpx.ASGITransport(app=ingress, client=("192.0.2.99", 1))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        assert (await client.get("/api/health")).status_code == 403
    # Directly: middleware stack includes our class with enabled=True.
    found = False
    for cls, args, kwargs in ingress.user_middleware:
        if cls is IngressOnlyMiddleware or getattr(cls, "__name__", "") == "IngressOnlyMiddleware":
            # add_middleware(IngressOnlyMiddleware, enabled=ingress_only)
            if kwargs.get("enabled") is True or (args and args[0] is True):
                found = True
    # Starlette 0.x stores Middleware(cls, **kwargs)
    if not found:
        for item in ingress.user_middleware:
            opts = getattr(item, "kwargs", {})
            cls = getattr(item, "cls", None)
            if cls is IngressOnlyMiddleware and opts.get("enabled") is True:
                found = True
    assert found, ingress.user_middleware
    assert link is not None
