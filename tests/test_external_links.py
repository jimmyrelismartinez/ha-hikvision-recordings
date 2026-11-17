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
    """Values above 900 are rejected (ok=False) and clamped back to the default."""
    from hikvision_recordings.app.config import LINK_TTL_DEFAULT

    ok = load_config({**OPTIONS, "external_link_ttl_s": 900})
    assert ok.external_link_ttl_s == 900
    assert ok.external_links_ok is True

    over = load_config({**OPTIONS, "external_link_ttl_s": 901})
    assert over.external_link_ttl_s == LINK_TTL_DEFAULT
    assert over.external_links_ok is False


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
    """Access log is off; TRACE must never print the token; link level is clamped."""
    from hikvision_recordings.app.serve import _link_log_level

    assert _link_log_level("trace") == "info"
    assert _link_log_level("debug") == "info"
    assert _link_log_level("warning") == "warning"
    assert _link_log_level("info") == "info"

    (ingress, link), _ = _apps()
    _freeze_clock(ingress, NOW)
    with TestClient(ingress) as client:
        body = client.post(
            "/internal/mint", json={"camera": "dvr-101"}, headers=_auth(),
        ).json()
    token = body["url"].split("/m/")[1].split("/")[0]
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
        # TRACE is not a stdlib level; use the numeric value uvicorn uses (5).
        logging.getLogger("uvicorn").setLevel(5)
        logging.getLogger("uvicorn.access").setLevel(5)
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
    """Exercise serve.py: capture uvicorn Configs and assert middleware + link opts."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "launcher-token-not-real")
    monkeypatch.setenv("LOG_LEVEL", "trace")

    captured: list = []

    class _FakeServer:
        def __init__(self, config):
            captured.append(config)
            self.config = config
            self.should_exit = False

        async def serve(self):
            self.should_exit = True

    monkeypatch.setattr("hikvision_recordings.app.serve.uvicorn.Server", _FakeServer)
    monkeypatch.setattr(
        "hikvision_recordings.app.serve.load_config_from_file",
        lambda: _cfg(),
    )
    monkeypatch.setattr(
        "hikvision_recordings.app.serve.create_apps",
        lambda config: create_apps(config, client=FakeClient()),
    )

    from hikvision_recordings.app.serve import _run

    await _run()

    assert "SUPERVISOR_TOKEN" not in os.environ
    assert len(captured) == 2
    ingress_cfg, link_cfg = captured
    assert ingress_cfg.port == 8099
    assert link_cfg.port == 8100
    assert link_cfg.access_log is False
    assert link_cfg.server_header is False
    assert link_cfg.lifespan == "off"
    assert link_cfg.log_level == "info"  # trace clamped

    ingress_app = ingress_cfg.app
    found = False
    for item in ingress_app.user_middleware:
        opts = getattr(item, "kwargs", {})
        cls = getattr(item, "cls", None)
        if cls is IngressOnlyMiddleware and opts.get("enabled") is True:
            found = True
    assert found, ingress_app.user_middleware

    transport = httpx.ASGITransport(app=ingress_app, client=("192.0.2.99", 1))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        assert (await client.get("/api/health")).status_code == 403


def test_link_server_bind_failure_does_not_stop_the_process():
    """uvicorn raises SystemExit(1) on a port clash; serve_isolated must swallow it."""
    from hikvision_recordings.app.serve import serve_isolated

    class _ClashingServer:
        async def serve(self):
            raise SystemExit(1)

    class _CrashingServer:
        async def serve(self):
            raise RuntimeError("boom")

    asyncio.run(serve_isolated(_ClashingServer()))
    asyncio.run(serve_isolated(_CrashingServer()))


# ── review regressions ───────────────────────────────────────────────────────

def test_sweep_does_not_delete_live_or_inflight_stages(staging):
    """Periodic sweep must not 410 live links; in-flight stage files stay live."""
    (ingress, link), _ = _apps()
    cache = ingress.state.link_cache
    _freeze_clock(ingress, NOW)
    with TestClient(ingress) as client:
        body = client.post(
            "/internal/mint", json={"camera": "dvr-101"}, headers=_auth(),
        ).json()
    token = body["url"].split("/m/")[1].split("/")[0]
    live_before = list(staging.glob("hiklink-*"))
    assert live_before
    cache.sweep(staging)
    with StarletteTestClient(link) as client:
        assert client.get(f"/m/{token}/clip.mp4").status_code == 200
    assert all(p.exists() for p in live_before)

    # In-flight: path recorded before bytes finish; sweep must not unlink it.
    gate = asyncio.Event()
    released = asyncio.Event()

    async def slow_stage(clip, quality, on_path=None):
        path = staging / "hiklink-inflight-test.mp4"
        path.write_bytes(b"partial")
        if on_path is not None:
            on_path(path)
        released.set()
        await gate.wait()
        path.write_bytes(PAYLOAD)
        return path

    cache2 = type(cache)(slow_stage, time_fn=cache._time, max_entries=2)
    from hikvision_recordings.app.clips import Clip, KIND_DVR

    clip = Clip(
        camera_key="dvr-101", kind=KIND_DVR, channel=101,
        start=NOW - timedelta(minutes=3), end=NOW - timedelta(minutes=2),
        size_bytes=100, main_ref=RECORDING.playback_uri + "&inflight=1",
        events=("motion",),
    )

    async def _run():
        task = asyncio.create_task(
            cache2.acquire(clip, "sd", needs_stage=True, ttl_s=300)
        )
        await released.wait()
        assert (staging / "hiklink-inflight-test.mp4").exists()
        cache2.sweep(staging)
        assert (staging / "hiklink-inflight-test.mp4").exists()
        gate.set()
        entry, token_str, _ = await task
        assert entry.path is not None and entry.path.exists()
        assert token_str

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_overlapping_mints_cap_slots_and_files(staging, monkeypatch):
    """3 overlapping mints: ≤2 hiklink files; never a token for an untracked entry."""
    from hikvision_recordings.app.link_cache import LinkCacheBusy, LinkStageCache
    from hikvision_recordings.app.clips import Clip, KIND_DVR

    started = asyncio.Event()
    release = asyncio.Event()
    active = 0
    max_active = 0
    lock = asyncio.Lock()

    async def gated_stage(clip, quality, on_path=None):
        nonlocal active, max_active
        path = staging / f"hiklink-{clip.main_ref.split('n=')[-1]}.mp4"
        path.write_bytes(b"x")
        if on_path is not None:
            on_path(path)
        async with lock:
            active += 1
            max_active = max(max_active, active)
            if active >= 2:
                started.set()
        await release.wait()
        async with lock:
            active -= 1
        path.write_bytes(PAYLOAD)
        return path

    cache = LinkStageCache(gated_stage, max_entries=2, time_fn=lambda: NOW.timestamp())

    def make_clip(i: int) -> Clip:
        start = NOW - timedelta(minutes=10 + i)
        return Clip(
            camera_key="dvr-101", kind=KIND_DVR, channel=101,
            start=start, end=start + timedelta(seconds=30),
            size_bytes=100,
            main_ref=f"rtsp://192.0.2.50/Streaming/tracks/101/?n={i}&size=100",
            events=("motion",),
        )

    async def one(i: int):
        try:
            entry, token, _ = await cache.acquire(
                make_clip(i), "sd", needs_stage=True, ttl_s=300,
            )
            return ("ok", entry.id, token, entry.id in cache._by_id)
        except LinkCacheBusy:
            return ("busy", None, None, None)

    t0 = asyncio.create_task(one(0))
    t1 = asyncio.create_task(one(1))
    await started.wait()
    t2 = asyncio.create_task(one(2))
    # Let the third attempt hit the full staging slots.
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(t0, t1, t2)

    oks = [r for r in results if r[0] == "ok"]
    busys = [r for r in results if r[0] == "busy"]
    assert len(oks) == 2
    assert len(busys) == 1
    for _, eid, token, tracked in oks:
        assert token and tracked and eid in cache._by_id
    files = list(staging.glob("hiklink-*"))
    assert len(files) <= 2


def test_mint_without_since_prefers_newest_over_paged_oldest(staging, monkeypatch):
    """>max_search_results clips: trailing 2 h window keeps the newest."""
    max_n = 5
    monkeypatch.setattr(
        "hikvision_recordings.app.main.load_config_from_file",
        lambda: _cfg(max_search_results=max_n),
    )
    # Build many old clips outside the last 2 h, plus one newest inside 2 h.
    recs = []
    for i in range(max_n + 3):
        # Old window: 3–20 h ago (outside trailing 2 h).
        start = NOW - timedelta(hours=3, minutes=i)
        end = start + timedelta(seconds=30)
        recs.append(Recording(
            channel=101, start=start, end=end, size_bytes=100,
            playback_uri=f"rtsp://192.0.2.50/Streaming/tracks/101/?old={i}&size=100",
            events=("motion",),
        ))
    newest_start = NOW - timedelta(minutes=30)
    newest_end = newest_start + timedelta(seconds=45)
    newest = Recording(
        channel=101, start=newest_start, end=newest_end, size_bytes=100,
        playback_uri="rtsp://192.0.2.50/Streaming/tracks/101/?newest=1&size=100",
        events=("motion",),
    )
    recs.append(newest)

    class PagedFake(FakeClient):
        """Honor max_results like a DVR page: oldest-first, capped per call."""

        async def search(self, channel, start_utc, end_utc, max_results=None):
            self.calls.append((channel, start_utc, end_utc, max_results))
            in_window = sorted(
                [r for r in self.recordings
                 if r.channel == channel and r.start >= start_utc and r.start < end_utc],
                key=lambda r: r.start,
            )
            if max_results is not None:
                in_window = in_window[:max_results]
            return in_window

    cfg = _cfg(max_search_results=max_n, max_results=max_n)
    fake = PagedFake(recordings=recs)
    ingress, link = create_apps(cfg, client=fake)
    _freeze_clock(ingress, NOW)
    with TestClient(ingress) as client:
        r = client.post(
            "/internal/mint", json={"camera": "dvr-101"}, headers=_auth(),
        )
    assert r.status_code == 200, r.text
    assert r.json()["start"] == newest_start.isoformat().replace("+00:00", "Z")
    # First search should be the trailing 2 h window.
    assert any(
        (end - start) <= timedelta(hours=2, seconds=1)
        for _, start, end, _ in fake.calls
    )


@pytest.mark.asyncio
async def test_two_poll_prefers_newest_until_deadline(staging, monkeypatch):
    """If newest is not yet stable, keep polling; fall back to older only at deadline."""
    newest_start = NOW - timedelta(seconds=40)
    older_start = NOW - timedelta(minutes=5)
    uri_n = "rtsp://192.0.2.50/Streaming/tracks/101/?starttime=n&size=100"
    uri_o = "rtsp://192.0.2.50/Streaming/tracks/101/?starttime=o&size=100"
    holder: dict = {"polls": 0}

    class PollFake(FakeClient):
        async def search(self, channel, start_utc, end_utc, max_results=None):
            holder["polls"] += 1
            polls = holder["polls"]
            # Advance the fake clock each poll so wait_s can expire.
            app = holder["app"]
            app.state.clock.freeze(NOW + timedelta(seconds=5 * (polls - 1)))
            older = Recording(
                channel=101, start=older_start,
                end=NOW - timedelta(minutes=4),
                size_bytes=100, playback_uri=uri_o, events=("motion",),
            )
            if polls < 3:
                # Newest end still changing — not confirmed stable.
                newest_end = NOW - timedelta(seconds=10 + polls)
            else:
                newest_end = NOW - timedelta(seconds=20)
            newest = Recording(
                channel=101, start=newest_start, end=newest_end,
                size_bytes=100, playback_uri=uri_n, events=("motion",),
            )
            self.recordings = [older, newest]
            return [older, newest]

    fake = PollFake(recordings=[])
    (ingress, _), _ = _apps(fake)
    holder["app"] = ingress
    _freeze_clock(ingress, NOW)

    async def fast_sleep(_):
        return None

    monkeypatch.setattr(main_module.asyncio, "sleep", fast_sleep)

    transport = httpx.ASGITransport(app=ingress)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.post(
            "/internal/mint",
            json={
                "camera": "dvr-101",
                "since": newest_start.isoformat().replace("+00:00", "Z"),
                "wait_s": 30,
            },
            headers=_auth(),
        )
    assert r.status_code == 200, r.text
    assert r.json()["start"] == newest_start.isoformat().replace("+00:00", "Z")
    assert holder["polls"] >= 3


@pytest.mark.asyncio
async def test_two_poll_falls_back_to_older_at_deadline(staging, monkeypatch):
    newest_start = NOW - timedelta(seconds=40)
    older_start = NOW - timedelta(minutes=5)
    uri_n = "rtsp://192.0.2.50/Streaming/tracks/101/?starttime=n2&size=100"
    uri_o = "rtsp://192.0.2.50/Streaming/tracks/101/?starttime=o2&size=100"
    holder: dict = {"polls": 0}

    class PollFake(FakeClient):
        async def search(self, channel, start_utc, end_utc, max_results=None):
            holder["polls"] += 1
            polls = holder["polls"]
            app = holder["app"]
            # Jump to deadline on second poll so newest never confirms.
            app.state.clock.freeze(NOW + timedelta(seconds=30 if polls > 1 else 0))
            older = Recording(
                channel=101, start=older_start,
                end=NOW - timedelta(minutes=4),
                size_bytes=100, playback_uri=uri_o, events=("motion",),
            )
            newest = Recording(
                channel=101, start=newest_start,
                end=NOW - timedelta(seconds=10 + polls),  # always changing
                size_bytes=100, playback_uri=uri_n, events=("motion",),
            )
            return [older, newest]

    fake = PollFake(recordings=[])
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
                "since": older_start.isoformat().replace("+00:00", "Z"),
                "wait_s": 20,
            },
            headers=_auth(),
        )
    assert r.status_code == 200, r.text
    assert r.json()["start"] == older_start.isoformat().replace("+00:00", "Z")


def test_mint_error_mapping_no_exception_text(staging, monkeypatch):
    from hikvision_recordings.app.isapi import DvrBusy, DvrUnreachable
    from hikvision_recordings.app.remux import RemuxError

    (ingress, _), _ = _apps()
    _freeze_clock(ingress, NOW)

    # invalid since
    with TestClient(ingress) as client:
        bad_since = client.post(
            "/internal/mint",
            json={"camera": "dvr-101", "since": "not-a-time"},
            headers=_auth(),
        )
    assert bad_since.status_code == 400
    assert bad_since.json() == {"detail": "bad_request"}

    # DvrBusy during search/polling
    class BusyFake(FakeClient):
        async def search(self, *a, **k):
            raise DvrBusy("DVR is busy serving another stream.")

    (ingress_b, _), _ = _apps(BusyFake())
    _freeze_clock(ingress_b, NOW)
    with TestClient(ingress_b) as client:
        busy = client.post(
            "/internal/mint", json={"camera": "dvr-101"}, headers=_auth(),
        )
    assert busy.status_code == 503
    assert busy.json() == {"detail": "busy"}
    assert "DVR" not in busy.text

    # Staging RemuxError → busy
    (ingress_r, _), _ = _apps()
    _freeze_clock(ingress_r, NOW)

    async def boom(clip, quality, on_path=None):
        raise RemuxError("ffmpeg failed loudly")

    ingress_r.state.link_cache._stage_fn = boom
    with TestClient(ingress_r) as client:
        remux = client.post(
            "/internal/mint", json={"camera": "dvr-101"}, headers=_auth(),
        )
    assert remux.status_code == 503
    assert remux.json() == {"detail": "busy"}
    assert "ffmpeg" not in remux.text

    # Generic DvrError during search
    class UnreachFake(FakeClient):
        async def search(self, *a, **k):
            raise DvrUnreachable("host 192.0.2.50 down")

    (ingress_u, _), _ = _apps(UnreachFake())
    _freeze_clock(ingress_u, NOW)
    with TestClient(ingress_u) as client:
        unreach = client.post(
            "/internal/mint", json={"camera": "dvr-101"}, headers=_auth(),
        )
    assert unreach.status_code == 503
    assert unreach.json() == {"detail": "busy"}
    assert "192.0.2.50" not in unreach.text


def test_mint_skips_registry_put(staging, monkeypatch):
    """Mint must not call registry.put (would wipe UI sub pairing)."""
    from hikvision_recordings.app import registry as reg_mod

    puts = []
    original = reg_mod.ClipRegistry.put

    def spy(self, clip):
        puts.append(clip)
        return original(self, clip)

    monkeypatch.setattr(reg_mod.ClipRegistry, "put", spy)
    (ingress, _), _ = _apps()
    _freeze_clock(ingress, NOW)
    puts.clear()
    with TestClient(ingress) as client:
        r = client.post(
            "/internal/mint", json={"camera": "dvr-101"}, headers=_auth(),
        )
    assert r.status_code == 200
    assert puts == []


def test_dvr_link_gone_when_file_vanishes(staging):
    (ingress, link), _ = _apps()
    _freeze_clock(ingress, NOW)
    with TestClient(ingress) as client:
        body = client.post(
            "/internal/mint", json={"camera": "dvr-101"}, headers=_auth(),
        ).json()
    token = body["url"].split("/m/")[1].split("/")[0]
    entry = ingress.state.link_cache.get(
        verify_token(ingress.state.link_cache.signing_key, token)["e"]
    )
    entry.path.unlink()
    with StarletteTestClient(link) as client:
        r = client.get(f"/m/{token}/clip.mp4")
    assert r.status_code == 410
    assert r.json() == {"detail": "Gone"}


def test_reolink_link_head_and_range(staging):
    from hikvision_recordings.app.clips import Clip, KIND_REOLINK
    from hikvision_recordings.app.config import ReolinkCamera
    from hikvision_recordings.app.isapi import DvrBusy
    from hikvision_recordings.app.reolink import Upstream

    MAIN_REF = (
        "Mp4Record/2026-11-17/RecM0A_20261117_113000_113030_0_"
        "9B28C080000000_41D8273.mp4"
    )
    SUB_REF = (
        "Mp4Record/2026-11-17/RecS0A_20261117_113000_113030_0_"
        "5514C080000000_65A424.mp4"
    )
    payload = b"REOLINK" * 200
    row = {"name": "Porch", "host": "192.0.2.40", "username": "v", "password": "pw"}
    clip = Clip(
        camera_key="reolink-0", kind=KIND_REOLINK, channel=0,
        start=NOW - timedelta(minutes=10),
        end=NOW - timedelta(minutes=9, seconds=30),
        size_bytes=len(payload), main_ref=MAIN_REF,
        sub_ref=SUB_REF, sub_size_bytes=900, events=("motion",),
    )

    class FakeReolink:
        kind = KIND_REOLINK
        key = "reolink-0"

        def __init__(self):
            self.camera = ReolinkCamera(**row)
            self.clock = SimpleNamespace(
                offset=timedelta(0), source="configured", drift=timedelta(0),
            )
            self.opened = []
            self.clips = [clip]

        async def probe_clock(self):
            return self.clock

        async def ping(self):
            return None

        async def search(self, start_utc, end_utc, limit):
            return list(self.clips), False

        async def open(self, ref, range_header=None, clamp=False, busy_timeout=None):
            self.opened.append((ref, range_header, clamp, busy_timeout))

            async def body():
                if range_header and range_header.startswith("bytes="):
                    spec = range_header.split("=", 1)[1]
                    start_s, _, end_s = spec.partition("-")
                    start_i = int(start_s or 0)
                    end_i = int(end_s) if end_s else len(payload) - 1
                    yield payload[start_i : end_i + 1]
                else:
                    yield payload

            async def closer():
                return None

            status = 206 if range_header else 200
            headers = {"content-length": str(len(payload))}
            if range_header:
                headers["content-range"] = (
                    f"bytes 0-{len(payload) - 1}/{len(payload)}"
                )
            return Upstream(status=status, headers=headers, body=body(), _closer=closer)

        async def aclose(self):
            return None

    cfg = load_config({
        "dvr_host": "",
        "channels": [],
        "reolink_cameras": [row],
        "external_links": True,
        "external_mint_token": MINT_TOKEN,
        "external_link_ttl_s": 300,
        "external_base_url": BASE_URL,
    })
    fake = FakeReolink()
    ingress, link = create_apps(cfg, reolink_clients=[fake])
    assert link is not None
    _freeze_clock(ingress, NOW)
    with TestClient(ingress) as client:
        body = client.post(
            "/internal/mint",
            json={"camera": "reolink-0", "quality": "hd"},
            headers=_auth(),
        ).json()
    token = body["url"].split("/m/")[1].split("/")[0]
    opened_before = list(fake.opened)
    with StarletteTestClient(link) as client:
        head = client.head(f"/m/{token}/clip.mp4")
        assert head.status_code == 200
        assert head.headers["content-length"] == str(len(payload))
        # HEAD must not contact the camera.
        assert fake.opened == opened_before
        ranged = client.get(
            f"/m/{token}/clip.mp4", headers={"Range": "bytes=0-6"},
        )
    assert ranged.status_code == 206
    assert ranged.content == payload[:7]
    assert fake.opened[-1][0] == MAIN_REF
    assert fake.opened[-1][2] is True

    # Error body is JSON with fixed detail (no camera name / repr).
    async def busy_open(*a, **k):
        raise DvrBusy(f"The camera {fake.camera.name} is busy.")

    fake.open = busy_open
    with StarletteTestClient(link) as client:
        err = client.get(f"/m/{token}/clip.mp4")
    assert err.status_code == 503
    assert err.json() == {"detail": "busy"}
    assert "Porch" not in err.text


def test_hard_cap_eviction_with_fake_time(staging):
    (ingress, link), _ = _apps(external_link_ttl_s=900)
    cache = ingress.state.link_cache
    _freeze_clock(ingress, NOW)
    with TestClient(ingress) as client:
        body = client.post(
            "/internal/mint", json={"camera": "dvr-101"}, headers=_auth(),
        ).json()
    token = body["url"].split("/m/")[1].split("/")[0]
    payload = verify_token(cache.signing_key, token)
    entry = cache.get(payload["e"])
    assert entry is not None
    staged = entry.path
    assert staged is not None and staged.exists()
    # Advance past the 900 s hard cap; sweep must drop the entry and unlink.
    _freeze_clock(ingress, NOW + timedelta(seconds=901))
    cache.sweep()
    assert cache.get(payload["e"]) is None
    assert not staged.exists()
    # A still-signed token for the evicted id is 410 (not a re-stage).
    from hikvision_recordings.app.token import mint as mint_token
    still_signed = mint_token(
        cache.signing_key, payload["e"], int(ingress.state.clock.time()) + 60,
    )
    with StarletteTestClient(link) as client:
        assert client.get(f"/m/{still_signed}/clip.mp4").status_code == 410
        # Original token is past its own exp as well.
        assert client.get(f"/m/{token}/clip.mp4").status_code == 403


def test_startup_cleanup_of_leftover_hiklink_files(staging, monkeypatch):
    leftover = staging / "hiklink-leftover-from-crash.mp4"
    leftover.write_bytes(b"orphan")
    (ingress, _), _ = _apps()
    # Lifespan runs cleanup_files(older_than=0) on startup.
    with TestClient(ingress):
        pass
    assert not leftover.exists()


def test_wait_s_cap_is_120_seconds(staging):
    """On-device: a motion clip closes 30-70 s after the trigger and is listed ~20 s
    later, so 60 s was too short. 120 is accepted; anything above is a bad request."""
    assert main_module.MINT_WAIT_MAX_S == 120
    (ingress, _), _ = _apps()
    _freeze_clock(ingress, NOW)
    with TestClient(ingress) as client:
        r = client.post(
            "/internal/mint",
            json={"camera": "dvr-101", "wait_s": 121},
            headers=_auth(),
        )
    assert r.status_code == 400
    assert r.json()["detail"] == "bad_request"
