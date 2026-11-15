"""ReolinkClient against a fake camera built from the live probe (2026-11-14).

The fake reproduces the verified firmware behaviour, quirks included: a Search
window that crosses midnight returns NO files, zero-length stub files exist, a
bad token is a 401 text/html, a missing file a 404 HTML page, and single byte
ranges are honoured (416 past the end).
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from hikvision_recordings.app import reolink as reolink_module
from hikvision_recordings.app.config import ReolinkCamera
from hikvision_recordings.app.isapi import (
    ClipGone,
    DvrAuthError,
    DvrBadRequest,
    DvrBusy,
    RangeNotSatisfiable,
)
from hikvision_recordings.app.reolink import (
    SLICE_BYTES,
    InvalidReolinkRef,
    ReolinkClient,
    day_spans,
    sanitize_range,
)

CAM = ReolinkCamera(name="Porch", host="192.0.2.40", username="viewer", password="pw-secret")
BASE = "http://192.0.2.40:80"
# Camera clock reads 19:22:19 local while our UTC is 01:22:19 next day -> UTC-6, no DST now.
NOW_UTC = datetime(2026, 11, 15, 1, 22, 19, tzinfo=timezone.utc)


def _t(dt: datetime) -> dict:
    return {"year": dt.year, "mon": dt.month, "day": dt.day,
            "hour": dt.hour, "min": dt.minute, "sec": dt.second}


def _file(stream: str, start: datetime, end: datetime, size=1_000_000, dst=False) -> dict:
    letter = "M" if stream == "main" else "S"
    marker = "DST" if dst else ""
    name = (f"Mp4Record/{start:%Y-%m-%d}/Rec{letter}0A_{marker}{start:%Y%m%d_%H%M%S}_"
            f"{end:%H%M%S}_0_9B28C080000000_41D8273.mp4")
    return {"name": name, "type": stream, "size": str(size),
            "StartTime": _t(start), "EndTime": _t(end),
            "width": 0, "height": 0, "frameRate": 0}


class FakeCamera:
    def __init__(self, files=None, body=b"\x00\x00\x00\x20ftypmp42" + b"v" * 4000,
                 password="pw-secret", now_local=datetime(2026, 11, 14, 19, 22, 19)):
        self.files = files or []
        self.body = body
        self.password = password
        self.now_local = now_local
        self.tokens_issued = 0
        self.valid_tokens: set[str] = set()
        self.calls: list[str] = []
        self.searches: list[tuple[str, datetime, datetime]] = []
        self.download_ranges: list[str | None] = []
        self.logged_out: list[str] = []
        self.missing: set[str] = set()
        self.html_downloads = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        cmd = request.url.params.get("cmd")
        token = request.url.params.get("token")
        self.calls.append(cmd)
        if cmd == "Login":
            user = json.loads(request.content)[0]["param"]["User"]
            if user["password"] != self.password:
                return httpx.Response(200, json=[{"cmd": "Login", "code": 1, "error": {
                    "detail": "login failed", "rspCode": -7}}])
            self.tokens_issued += 1
            tok = f"tok{self.tokens_issued}"
            self.valid_tokens.add(tok)
            return httpx.Response(200, json=[{"cmd": "Login", "code": 0, "value": {
                "Token": {"leaseTime": 3600, "name": tok}}}])
        if token not in self.valid_tokens:
            if cmd == "Download":
                return httpx.Response(401, headers={"content-type": "text/html"}, text="")
            return httpx.Response(200, json=[{"cmd": cmd, "code": 1, "error": {
                "detail": "please login first", "rspCode": -6}}])
        if cmd == "Logout":
            self.logged_out.append(token)
            self.valid_tokens.discard(token)
            return httpx.Response(200, json=[{"cmd": "Logout", "code": 0, "value": {"rspCode": 200}}])
        if cmd == "GetTime":
            return httpx.Response(200, json=[{"cmd": "GetTime", "code": 0, "value": {
                "Dst": {"enable": 1, "offset": 1},
                "Time": {**_t(self.now_local), "isDst": 0, "timeZone": 21600}}}])
        if cmd == "Search":
            p = json.loads(request.content)
            assert len(p) == 1, "batched Search fails on this firmware — one per request"
            s = p[0]["param"]["Search"]
            lo = datetime(**{k2: s["StartTime"][k1] for k1, k2 in (
                ("year", "year"), ("mon", "month"), ("day", "day"),
                ("hour", "hour"), ("min", "minute"), ("sec", "second"))})
            hi = datetime(**{k2: s["EndTime"][k1] for k1, k2 in (
                ("year", "year"), ("mon", "month"), ("day", "day"),
                ("hour", "hour"), ("min", "minute"), ("sec", "second"))})
            self.searches.append((s["streamType"], lo, hi))
            if lo.date() != hi.date():
                files = []          # VERIFIED QUIRK: a span over midnight finds nothing
            else:
                files = [f for f in self.files if f["type"] == s["streamType"]
                         and datetime(**_kw(f["EndTime"])) >= lo
                         and datetime(**_kw(f["StartTime"])) <= hi]
            result = {"Status": [{"mon": 11, "table": "1" * 14, "year": 2026}]}
            if files:
                result["File"] = files
            return httpx.Response(200, json=[{"cmd": "Search", "code": 0,
                                              "value": {"SearchResult": result}}])
        if cmd == "Download":
            source = request.url.params.get("source")
            if self.html_downloads:
                return httpx.Response(200, headers={"content-type": "text/html"}, text="<html/>")
            if source in self.missing:
                return httpx.Response(404, headers={"content-type": "text/html"},
                                      text="<html>404 Not Found</html>")
            rng = request.headers.get("range")
            self.download_ranges.append(rng)
            total = len(self.body)
            if rng:
                first, last = rng.removeprefix("bytes=").split("-")
                if not first:
                    a, b = total - int(last), total - 1
                else:
                    a, b = int(first), int(last) if last else total - 1
                if a >= total:
                    return httpx.Response(416, headers={"content-range": f"bytes */{total}"})
                b = min(b, total - 1)
                return httpx.Response(206, content=self.body[a:b + 1], headers={
                    "content-type": "apolication/octet-stream",
                    "content-range": f"bytes {a}-{b}/{total}"})
            return httpx.Response(200, content=self.body, headers={
                "content-type": "apolication/octet-stream"})
        return httpx.Response(404)


def _kw(t: dict) -> dict:
    return {"year": t["year"], "month": t["mon"], "day": t["day"],
            "hour": t["hour"], "minute": t["min"], "second": t["sec"]}


@pytest.fixture
def camera():
    return FakeCamera()


@pytest.fixture
def mocked(camera):
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        router.route().mock(side_effect=camera.handler)
        yield camera


async def _client(max_concurrent=2) -> ReolinkClient:
    rc = ReolinkClient(CAM, "reolink-0", max_concurrent)
    await rc.probe_clock(now_utc=NOW_UTC)
    return rc


# ── pure helpers ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("header,clamp,expected", [
    (None, True, None),
    ("bytes=0-1023", True, "bytes=0-1023"),
    ("bytes=100-", False, "bytes=100-"),
    ("bytes=100-", True, f"bytes=100-{100 + SLICE_BYTES - 1}"),
    ("bytes=0-99999999", True, "bytes=0-99999999"),   # closed: exact (AVFoundation)
    ("bytes=-500", True, "bytes=-500"),
    ("bytes=0-10,20-30", True, None),      # multi-range: malformed 206 on this firmware
    ("bytes=10-5", True, None),
    ("items=0-10", True, None),
    ("bytes=-", True, None),
    ("bytes=0-1\r\nX: y", True, None),
])
def test_sanitize_range(header, clamp, expected):
    assert sanitize_range(header, clamp) == expected


def test_day_spans_never_cross_midnight():
    spans = day_spans(datetime(2026, 11, 13, 19, 22, 0), datetime(2026, 11, 14, 19, 22, 0))
    assert spans == [
        (datetime(2026, 11, 13, 19, 22, 0), datetime(2026, 11, 13, 23, 59, 59)),
        (datetime(2026, 11, 14, 0, 0, 0), datetime(2026, 11, 14, 19, 22, 0)),
    ]


# ── session ──────────────────────────────────────────────────────────────────

async def test_token_is_reused_and_logged_out_on_close(mocked):
    rc = await _client()
    await rc.ping()
    await rc.ping()
    assert mocked.tokens_issued == 1
    await rc.aclose()
    assert mocked.logged_out == ["tok1"]


async def test_expired_token_triggers_one_relogin(mocked):
    rc = await _client()
    mocked.valid_tokens.clear()          # camera rebooted / lease dropped
    await rc.ping()
    assert mocked.tokens_issued == 2
    await rc.aclose()


async def test_bad_credentials_back_off_instead_of_hammering_the_camera(mocked):
    mocked.password = "something-else"
    rc = ReolinkClient(CAM, "reolink-0", 2)
    with pytest.raises(DvrAuthError):
        await rc.ping()
    logins = mocked.calls.count("Login")
    with pytest.raises(DvrAuthError):
        await rc.ping()
    assert mocked.calls.count("Login") == logins, "retried a rejected login immediately"
    await rc.aclose()


async def test_errors_never_carry_the_password_or_token(mocked):
    mocked.password = "x"
    rc = ReolinkClient(CAM, "reolink-0", 2)
    with pytest.raises(DvrAuthError) as info:
        await rc.ping()
    assert "pw-secret" not in str(info.value) and "tok" not in str(info.value)
    await rc.aclose()


# ── search ───────────────────────────────────────────────────────────────────

async def test_search_splits_at_midnight_pairs_sub_and_drops_stubs(mocked):
    # Camera-local times (UTC-6, no DST in effect now).
    mocked.files = [
        _file("main", datetime(2026, 11, 13, 21, 22, 9), datetime(2026, 11, 13, 21, 22, 21)),
        _file("sub", datetime(2026, 11, 13, 21, 22, 8), datetime(2026, 11, 13, 21, 22, 21), 50_000),
        _file("main", datetime(2026, 11, 14, 18, 35, 8), datetime(2026, 11, 14, 18, 36, 50)),
        _file("sub", datetime(2026, 11, 14, 18, 35, 6), datetime(2026, 11, 14, 18, 36, 50), 60_000),
        _file("main", datetime(2026, 11, 14, 19, 0, 0), datetime(2026, 11, 14, 19, 0, 0)),  # stub
    ]
    rc = await _client()
    clips, truncated = await rc.search(NOW_UTC - timedelta(hours=24), NOW_UTC, 500)
    await rc.aclose()

    assert all(lo.date() == hi.date() for _, lo, hi in mocked.searches)
    assert not truncated
    assert [c.start for c in clips] == [
        datetime(2026, 11, 14, 3, 22, 9, tzinfo=timezone.utc),
        datetime(2026, 11, 15, 0, 35, 8, tzinfo=timezone.utc),
    ]
    assert all(c.sub_ref and "/RecS0A_" in c.sub_ref for c in clips)
    assert clips[1].sub_size_bytes == 60_000
    assert all(c.camera_key == "reolink-0" and c.kind == "reolink" for c in clips)


async def test_search_window_filters_out_padding_matches(mocked):
    mocked.files = [
        _file("main", datetime(2026, 11, 14, 17, 0, 0), datetime(2026, 11, 14, 17, 0, 30)),
        _file("main", datetime(2026, 11, 14, 18, 30, 0), datetime(2026, 11, 14, 18, 30, 30)),
    ]
    rc = await _client()
    # 18:00-19:00 local == 00:00-01:00 UTC
    clips, _ = await rc.search(datetime(2026, 11, 15, 0, 0, tzinfo=timezone.utc),
                               datetime(2026, 11, 15, 1, 0, tzinfo=timezone.utc), 500)
    await rc.aclose()
    assert len(clips) == 1 and clips[0].start.hour == 0 and clips[0].start.minute == 30


async def test_dst_marked_files_convert_with_the_dst_offset(mocked):
    """Recorded 2026-11-01 00:37 CDT (UTC-5) while the camera is now on CST (UTC-6)."""
    mocked.files = [_file("main", datetime(2026, 11, 1, 0, 37, 27),
                          datetime(2026, 11, 1, 0, 37, 51), dst=True)]
    rc = await _client()
    clips, _ = await rc.search(datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc),
                               datetime(2026, 11, 1, 7, 0, tzinfo=timezone.utc), 500)
    await rc.aclose()
    assert [c.start for c in clips] == [datetime(2026, 11, 1, 5, 37, 27, tzinfo=timezone.utc)]


async def test_truncation_keeps_the_newest(mocked):
    base = datetime(2026, 11, 14, 10, 0, 0)
    mocked.files = [_file("main", base + timedelta(minutes=i), base + timedelta(minutes=i, seconds=20))
                    for i in range(5)]
    rc = await _client()
    clips, truncated = await rc.search(NOW_UTC - timedelta(hours=24), NOW_UTC, 3)
    await rc.aclose()
    assert truncated
    assert [c.start.minute for c in clips] == [2, 3, 4]


# ── bytes ────────────────────────────────────────────────────────────────────

REF = "Mp4Record/2026-11-14/RecM0A_20261114_183508_183650_0_9B28C080000000_41D8273.mp4"


async def test_open_forwards_a_clamped_range(mocked):
    rc = await _client()
    up = await rc.open(REF, "bytes=100-", clamp=True)
    data = b"".join([c async for c in up.body])
    await up.aclose()
    await rc.aclose()
    assert up.status == 206
    assert mocked.download_ranges == [f"bytes=100-{100 + SLICE_BYTES - 1}"]
    assert data == mocked.body[100:]
    assert up.headers["content-range"].endswith(f"/{len(mocked.body)}")


async def test_open_relogins_once_on_401(mocked):
    rc = await _client()
    mocked.valid_tokens.clear()
    up = await rc.open(REF)
    await up.aclose()
    await rc.aclose()
    assert up.status == 200 and mocked.tokens_issued == 2


async def test_missing_file_is_clip_gone(mocked):
    mocked.missing.add(REF)
    rc = await _client()
    with pytest.raises(ClipGone):
        await rc.open(REF)
    await rc.aclose()


async def test_range_past_the_end_is_416_with_content_range(mocked):
    rc = await _client()
    with pytest.raises(RangeNotSatisfiable) as info:
        await rc.open(REF, "bytes=999999999-")
    await rc.aclose()
    assert info.value.content_range == f"bytes */{len(mocked.body)}"


async def test_html_on_a_2xx_is_never_relayed_as_video(mocked):
    mocked.html_downloads = True
    rc = await _client()
    with pytest.raises(DvrBadRequest):
        await rc.open(REF)
    await rc.aclose()


async def test_hostile_ref_is_refused_before_any_request(mocked):
    rc = await _client()
    before = len(mocked.calls)
    with pytest.raises(InvalidReolinkRef):
        await rc.open("Mp4Record/x/../../etc/passwd")
    assert len(mocked.calls) == before
    await rc.aclose()


async def test_slot_is_released_even_if_the_body_is_never_read(mocked):
    rc = await _client(max_concurrent=1)
    up = await rc.open(REF)
    await up.aclose()                       # never iterated
    up2 = await asyncio.wait_for(rc.open(REF), timeout=1)
    await up2.aclose()
    await rc.aclose()


async def test_a_stalled_consumer_gives_the_slot_back(mocked, monkeypatch):
    """Goldfish B2: a paused <video> stops reading; the slot must not be held."""
    monkeypatch.setattr(reolink_module, "IDLE_READ_TIMEOUT_S", 0.05)
    monkeypatch.setattr(reolink_module, "CHUNK", 64)
    rc = await _client(max_concurrent=1)
    up = await rc.open(REF, "bytes=0-", clamp=True)
    body = up.body.__aiter__()
    await body.__anext__()                  # read one chunk, then "pause"
    await asyncio.sleep(0.2)
    up2 = await asyncio.wait_for(rc.open(REF), timeout=1)   # would be DvrBusy otherwise
    await up2.aclose()
    await up.aclose()
    await rc.aclose()


async def test_busy_when_all_slots_are_held(mocked, monkeypatch):
    monkeypatch.setattr(reolink_module, "BUSY_TIMEOUT_S", 0.05)
    rc = await _client(max_concurrent=1)
    up = await rc.open(REF)
    with pytest.raises(DvrBusy):
        await rc.open(REF)
    await up.aclose()
    await rc.aclose()


async def test_a_download_reader_may_pause_without_losing_the_file(mocked, monkeypatch):
    """No consumer watchdog on downloads: a slow 'save as?' must not truncate."""
    monkeypatch.setattr(reolink_module, "IDLE_READ_TIMEOUT_S", 0.05)
    monkeypatch.setattr(reolink_module, "CHUNK", 64)
    rc = await _client()
    up = await rc.open(REF)                 # clamp=False, as /api/download uses it
    body = up.body.__aiter__()
    first = await body.__anext__()
    await asyncio.sleep(0.2)
    rest = b"".join([c async for c in body])
    await up.aclose()
    await rc.aclose()
    assert first + rest == mocked.body


async def test_a_concurrent_401_does_not_discard_a_fresh_token(mocked):
    rc = await _client()
    stale = rc._token
    mocked.valid_tokens.clear()
    await rc.ping()                         # re-logs in -> tok2
    fresh = rc._token
    rc._drop_token(stale)                   # a second request failing on the stale one
    assert rc._token == fresh != stale
    await rc.aclose()
