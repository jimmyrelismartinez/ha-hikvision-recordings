#!/usr/bin/env python3
"""Answer the open questions about a Reolink camera's recording API, read-only.

Reads REOLINK_HOST / REOLINK_USER and REOLINK_PASS (or REOLINK_PASS_FILE) from the
environment. Prints findings only — never the password or the session token. The
only bytes written are the first few MB of one recording, to --out, so ffprobe can
answer the codec / container questions. Always logs out: Reolink cameras allow only
a handful of sessions and a leaked token holds one for up to an hour.

    REOLINK_HOST=192.0.2.40 REOLINK_USER=admin REOLINK_PASS_FILE=/path \
      .venv/bin/python scripts/verify_reolink_contract.py --hours 24 --out /tmp/reo
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

API = "/cgi-bin/api.cgi"


def _password() -> str:
    path = os.environ.get("REOLINK_PASS_FILE")
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    return os.environ["REOLINK_PASS"]


def _rtime(dt: datetime) -> dict:
    return {"year": dt.year, "mon": dt.month, "day": dt.day,
            "hour": dt.hour, "min": dt.minute, "sec": dt.second}


def _fmt(t: dict | None) -> str:
    if not t:
        return "?"
    return "{year:04d}-{mon:02d}-{day:02d}T{hour:02d}:{min:02d}:{sec:02d}".format(**t)


def _redact(obj):
    text = json.dumps(obj)
    return text[:600] + ("…" if len(text) > 600 else "")


def top_level_atoms(head: bytes) -> list[str]:
    """Walk MP4 top-level boxes inside the bytes we have. Tells us if moov is up front."""
    atoms, pos = [], 0
    while pos + 8 <= len(head):
        size, kind = struct.unpack(">I4s", head[pos:pos + 8])
        name = kind.decode("latin-1")
        if size == 1 and pos + 16 <= len(head):
            size = struct.unpack(">Q", head[pos + 8:pos + 16])[0]
        atoms.append(f"{name}({size})")
        if size < 8:
            break
        pos += size
    return atoms


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", type=int, default=0)
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--head-mb", type=int, default=4)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    host = os.environ["REOLINK_HOST"]
    user = os.environ["REOLINK_USER"]
    c = httpx.Client(base_url=f"http://{host}", timeout=httpx.Timeout(10.0, read=30.0))
    token = None

    def cmd(name: str, param: dict | None = None, action: int = 0):
        params = {"cmd": name}
        if token:
            params["token"] = token
        body = [{"cmd": name, "action": action, "param": param or {}}]
        r = c.post(API, params=params, json=body)
        r.raise_for_status()
        return r.json()[0]

    print(f"[ctx] host UTC   = {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}")
    print(f"[ctx] host local = {datetime.now():%Y-%m-%dT%H:%M:%S}")
    try:
        login = cmd("Login", {"User": {"userName": user, "password": _password()}})
        if login.get("code") != 0:
            print(f"[Q0 login] FAILED: {_redact(login)}")
            return 1
        token = login["value"]["Token"]["name"]
        print(f"[Q0 login] ok, leaseTime={login['value']['Token'].get('leaseTime')}s")

        for name, param in (("GetDevInfo", {}), ("GetTime", {}),
                            ("GetEnc", {"channel": args.channel}), ("GetHddInfo", {})):
            res = cmd(name, param)
            print(f"[Q1 {name}] code={res.get('code')} {_redact(res.get('value', res.get('error')))}")

        # --- Q2: search, main + sub, over the window (camera-local wall clock) ---
        dev_time = cmd("GetTime").get("value", {}).get("Time", {})
        now_local = datetime(dev_time["year"], dev_time["mon"], dev_time["day"],
                             dev_time["hour"], dev_time["min"], dev_time["sec"]) \
            if dev_time else datetime.now()
        start = now_local - timedelta(hours=args.hours)
        files_by_stream: dict[str, list] = {}
        # A window that crosses midnight returned 0 files live (2026-11-14) while the
        # day table said recordings existed, so compare one span vs per-day spans.
        spans = [(start, now_local)]
        day = start
        while day.date() <= now_local.date():
            lo = max(start, day.replace(hour=0, minute=0, second=0))
            hi = min(now_local, day.replace(hour=23, minute=59, second=59))
            spans.append((lo, hi))
            day = (day + timedelta(days=1)).replace(hour=0, minute=0, second=0)
        for stream in ("main", "sub"):
            files = []
            for i, (lo, hi) in enumerate(spans):
                res = cmd("Search", {"Search": {
                    "channel": args.channel, "onlyStatus": 0, "streamType": stream,
                    "StartTime": _rtime(lo), "EndTime": _rtime(hi)}})
                sr = res.get("value", {}).get("SearchResult", {})
                got = sr.get("File", []) or []
                label = "whole" if i == 0 else "day"
                print(f"[Q2 search {stream} {label} {lo:%m-%d %H:%M}..{hi:%m-%d %H:%M}] "
                      f"code={res.get('code')} files={len(got)}")
                if i > 0:
                    files.extend(got)
            files_by_stream[stream] = files
            print(f"[Q2 search {stream}] per-day total files={len(files)}")
            for f in files[:3] + (files[-2:] if len(files) > 5 else []):
                print(f"    name={f.get('name')!r} type={f.get('type')} size={f.get('size')} "
                      f"start={_fmt(f.get('StartTime'))} end={_fmt(f.get('EndTime'))} "
                      f"playback(UTC?)={_fmt(f.get('PlaybackTime'))} "
                      f"{f.get('width')}x{f.get('height')}@{f.get('frameRate')}")
            if files:
                print(f"    keys={sorted(files[0].keys())}")

        # --- Q3: download — Range support, container, codec, moov placement ---
        for stream, files in files_by_stream.items():
            if not files:
                continue
            # The smallest file was a zero-length stub (start == end, mdat of 8 bytes)
            # live; the median is a representative clip.
            f = sorted(files, key=lambda x: int(x.get("size") or 0))[len(files) // 2]
            name = f["name"]
            params = {"cmd": "Download", "source": name, "output": "probe.mp4", "token": token}
            r = c.get(API, params=params, headers={"Range": "bytes=0-1023"})
            print(f"[Q3 range {stream}] HTTP {r.status_code} "
                  f"len={r.headers.get('content-length')} "
                  f"content-range={r.headers.get('content-range')} "
                  f"type={r.headers.get('content-type')} "
                  f"accept-ranges={r.headers.get('accept-ranges')}")
            dest = args.out / f"head_{stream}.mp4"
            budget = args.head_mb * 1024 * 1024
            got = 0
            with c.stream("GET", API, params=params) as resp, dest.open("wb") as fh:
                print(f"[Q3 download {stream}] HTTP {resp.status_code} "
                      f"len={resp.headers.get('content-length')} (search size={f.get('size')}) "
                      f"type={resp.headers.get('content-type')} "
                      f"disposition={resp.headers.get('content-disposition')}")
                for chunk in resp.iter_bytes(262144):
                    fh.write(chunk)
                    got += len(chunk)
                    if got >= budget:
                        break
            head = dest.read_bytes()
            print(f"    wrote {got} bytes; first16={head[:16].hex()} atoms={top_level_atoms(head)}")
            if shutil.which("ffprobe"):
                p = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries",
                     "stream=codec_name,codec_type,width,height,r_frame_rate:format=format_name",
                     "-of", "compact", str(dest)], capture_output=True, text=True)
                print(f"    ffprobe rc={p.returncode} {p.stdout.strip()} {p.stderr.strip()[:300]}")
    finally:
        if token:
            try:
                res = cmd("Logout")
                print(f"[logout] code={res.get('code')}")
            except Exception as exc:  # noqa: BLE001 — report, never mask the real error
                print(f"[logout] failed: {exc}")
        c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
