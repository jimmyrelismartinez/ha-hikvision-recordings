#!/usr/bin/env python3
"""Answer the open questions in the design spec against the real DVR.

Reads DVR_HOST / DVR_USER / DVR_PASS from the environment. Prints findings only —
never the password, never any video. Run:

    DVR_HOST=10.10.11.56 DVR_USER=Jimmy DVR_PASS=... \
      .venv/bin/python scripts/verify_dvr_contract.py --channel 101 --hours 6
"""
from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import httpx

SEARCH_PATH = "/ISAPI/ContentMgmt/search"
DOWNLOAD_PATH = "/ISAPI/ContentMgmt/download"
TIME_PATH = "/ISAPI/System/time"


def client() -> httpx.Client:
    host = os.environ["DVR_HOST"]
    return httpx.Client(
        base_url=f"http://{host}",
        auth=httpx.DigestAuth(os.environ["DVR_USER"], os.environ["DVR_PASS"]),
        timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
    )


def search_body(channel: int, start: str, end: str, max_results: int, position: int) -> str:
    # searchID MUST be a uuid4 — see design spec 3.2. Anything else -> badXmlContent.
    return (
        "<CMSearchDescription>"
        f"<searchID>{uuid.uuid4()}</searchID>"
        f"<trackIDList><trackID>{channel}</trackID></trackIDList>"
        f"<timeSpanList><timeSpan><startTime>{start}</startTime>"
        f"<endTime>{end}</endTime></timeSpan></timeSpanList>"
        f"<maxResults>{max_results}</maxResults>"
        f"<searchResultPosition>{position}</searchResultPosition>"
        "</CMSearchDescription>"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", type=int, default=101)
    ap.add_argument("--hours", type=int, default=6)
    args = ap.parse_args()

    now_utc = datetime.now(timezone.utc)
    print(f"[ctx] host clock UTC = {now_utc:%Y-%m-%dT%H:%M:%SZ}")
    print(f"[ctx] host clock local = {datetime.now():%Y-%m-%dT%H:%M:%S}")

    with client() as c:
        # --- Q1: does the device report its own time/timezone? ---
        r = c.get(TIME_PATH)
        print(f"\n=== Q1 System/time -> HTTP {r.status_code} ===")
        print(r.text[:600])

        # --- Q2/Q4: search, thumbnails, paging field ---
        start = (now_utc - timedelta(hours=args.hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        body = search_body(args.channel, start, end, 40, 0)
        r = c.post(SEARCH_PATH, content=body.encode(),
                   headers={"Content-Type": "application/xml"})
        print(f"\n=== Q2 search {start} .. {end} -> HTTP {r.status_code} ===")
        print(r.text[:2500])
        print("\n[hint] Compare the returned <startTime>/starttime= values against the host "
              "clock lines above. If they track LOCAL wall-clock, the DVR labels local time "
              "with a bogus 'Z'. If they track UTC, 'Z' is honest.")
        print("[hint] Look for a picture/thumbnail element inside <searchMatchItem>.")
        print("[hint] Look for the paging/total field name (numOfMatches / totalMatches / "
              "responseStatusStrg='MORE'). Whatever it is called, copy it EXACTLY.")

        # --- Q3: does download honour Range? ---
        uri = None
        for line in r.text.splitlines():
            if "playbackURI" in line:
                uri = line.split(">", 1)[1].rsplit("<", 1)[0].replace("&amp;", "&")
                break
        if not uri:
            print("\n=== Q3 SKIPPED — no recordings in that window; widen --hours ===")
            return 0
        dl = f"<downloadRequest><playbackURI>{uri.replace('&', '&amp;')}</playbackURI></downloadRequest>"
        with c.stream("POST", DOWNLOAD_PATH, content=dl.encode(),
                      headers={"Content-Type": "application/xml",
                               "Range": "bytes=0-1023"}) as resp:
            got = next(resp.iter_bytes(2048), b"")
            print(f"\n=== Q3 download with Range: bytes=0-1023 -> HTTP {resp.status_code} ===")
            print(f"Accept-Ranges: {resp.headers.get('accept-ranges')!r}  "
                  f"Content-Range: {resp.headers.get('content-range')!r}  "
                  f"Content-Length: {resp.headers.get('content-length')!r}")
            print(f"first 4 bytes: {got[:4]!r}  (expect b'IMKH' = Hikvision MPEG-PS)")
            print(f"bytes actually delivered in first read: {len(got)}")
            print("[hint] 206 + Content-Range => Range honoured (seeking is possible). "
                  "200 + full length => not honoured; spec section 7 caveat is permanent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
