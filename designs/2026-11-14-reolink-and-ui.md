# Design: Reolink camera support + UI/functional enhancements (v0.2.0)

Status: DRAFT — for goldfish review (Elephant-Goldfish "lite": one doc, one fresh-context review)
Branch: `feat/reolink-and-ui`
Date: 2026-11-14

## 1. Problem

The add-on reads recordings from exactly one Hikvision DVR (`dvr_host`, `channels`).
The owner has added a **Reolink E1 Pro (E330)** Wi-Fi camera at `192.0.2.40` that records
to its own SD card. It is not on the DVR and does not speak ISAPI, so it cannot be added
as a `channels` row. The owner also asked for:

- **All-cameras view** — one merged, newest-first list across every camera/device.
- **Day timeline** — a strip per camera showing where recordings exist; tap to open.
- **Better player** — prev/next, autoplay-next, download from the player, keyboard.
- **Mobile & visual polish** — grid on wide screens, bigger touch targets, dark mode,
  clearer empty/error states.
- **SD/HD-aware download + faster thumbnails** (substream).
- **Paging** past `max_results` (required by the timeline — a full day must be complete).

Non-goals: recording anything, live view, PTZ, event-type filtering, other brands,
Reolink NVR/Home Hub (`NvrDownload`) — the adapter targets a standalone camera only.

## 2. Verified facts (live, 2026-11-14, `scripts/verify_reolink_contract.py`)

Device: `model=E1 Pro`, `itemNo=E Series E330`, `exactType=WIFI_SOLO_IPC`,
firmware `v3.1.0.4417_2412122130`, `channelNum=1`, one 64 GB SD card.

| Question | Answer |
|---|---|
| Local HTTP API? | Yes: `POST /cgi-bin/api.cgi?cmd=X[&token=T]`, JSON array body. Port 80 open, 443 closed. |
| Auth | `Login` → `Token.name`, `leaseTime=3600`. `Logout` works (code 0). Token goes in the query string. |
| Search | `cmd=Search`, `{channel:0, onlyStatus:0, streamType:"main"|"sub", StartTime{year,mon,day,hour,min,sec}, EndTime{…}}` → `SearchResult.File[]` with keys `name, size, type, StartTime, EndTime, width, height, frameRate` (width/height/frameRate are 0). **No `PlaybackTime` on this firmware.** |
| ⚠️ Midnight quirk | A window spanning midnight (11-13 19:22 → 11-14 19:22) returned **0 files**, while the same range split per calendar day returned 37 + 75. **Searches must be split per camera-local day.** |
| Result cap | 75 files in one day came back in one response; no paging/cap marker observed. (Unverified for very busy days — see §8.) |
| File names | `Mp4Record/2026-11-14/RecM0A_20261114_183508_183650_0_9B28C080000000_41D8273.mp4` (main), `…/RecS0A_…` (sub). Main/sub are listed **separately**, start a second or so apart. |
| Download | `GET /cgi-bin/api.cgi?cmd=Download&source=<name>&output=<x>.mp4&token=T` → 200, `Content-Length` = search `size`, `Content-Type: apolication/octet-stream` (sic — firmware typo). |
| Range | `Range: bytes=0-1023` → **206**, `Content-Range: bytes 0-1023/<total>`. No `Accept-Ranges` header. |
| Container/codec | **MP4, H.264 High + AAC**. Main 2880×1616 @ 5120 kbps, sub 896×512 @ 1024 kbps. `moov` is **at the front** (`ftyp, moov, skip, mdat`) — progressive/seekable. |
| Thumbnail | ffmpeg decoded a frame from the downloaded file directly. |
| Degenerate files | Some entries have `start == end` and an 8-byte `mdat` (≈271 KB file of nothing). Must be filtered or tolerated. |
| Clock | `GetTime` → camera-local wall clock fields + `timeZone=21600` (seconds *west*, i.e. UTC−6) + `Dst{enable:1,…}`, `isDst`. Search times are camera-local wall clock. |

**Consequence:** Reolink clips are browser-playable as-is. No remux (server or
ffmpeg.wasm), no staging — the add-on can **proxy bytes with Range passthrough** straight
to `<video>`, which also satisfies iOS (Content-Length + 206).

## 3. Technical plan

### 3.1 Config (backward compatible)

Supervisor does not migrate `options.json`, so existing keys stay exactly as they are.
Add one optional list:

```yaml
options:
  # …existing dvr_* / channels unchanged…
  reolink_cameras: []
schema:
  reolink_cameras:
    - name: str
      host: str
      port: "port?"          # default 80
      username: str
      password: password
      use_https: "bool?"     # default false
      channel: "int?"        # default 0
```

- `config.py`: new frozen `ReolinkCamera` dataclass; `Config.reolink_cameras: tuple[...]`.
- `dvr_host`/`dvr_username`/`dvr_password` become **optional when at least one Reolink
  camera is configured** (Reolink-only installs work). If `dvr_host` is empty, the DVR
  backend is simply not created and `channels` is ignored. At least one camera overall is
  still required. Existing configs validate unchanged.
- To verify on the HA box: Supervisor accepts `password` and `?` optional types inside a
  list-of-dicts schema, and an empty-list default.

### 3.2 Camera identity — string keys

Integer channel ids collide across devices, so every camera gets a string key:

- DVR channel → `dvr-101`, `dvr-301`, …
- Reolink → `reolink-0`, `reolink-1` (index in `reolink_cameras`).

`/api/channels` → `[{"key": "dvr-101", "name": "Driveway", "kind": "dvr"}, …]`.
`/api/recordings?camera=<key>|all` replaces `channel=<int>`. `channel=<int>` is still
accepted for one release and mapped to `dvr-<int>` (old cached frontends keep working
until the version-stamped reload). Download filenames use the camera name (already do).

### 3.3 Backend abstraction

New module `app/backends.py`:

```python
@dataclass(frozen=True)
class Clip:                    # replaces direct use of isapi.Recording above the backend
    camera_key: str
    start: datetime            # UTC, tz-aware
    end: datetime
    size_bytes: int
    main_ref: str              # backend-opaque: Hikvision playbackURI / Reolink file name
    sub_ref: str | None        # paired substream ref when known at search time
    sub_size_bytes: int | None

class Backend(Protocol):
    kind: str                                    # "dvr" | "reolink"
    async def search(self, camera_key, start_utc, end_utc, limit) -> SearchResult  # clips + truncated
    async def open(self, clip, quality, range_header) -> ByteSource   # status, headers, async iterator
    def validate_ref(self, ref: str) -> None     # trust boundary, raises InvalidRef
    def native_mp4(self) -> bool                 # True → direct playback, no remux
    async def ping(self) -> str                  # "ok" | "auth_failed" | "unreachable"
    clock: DvrClock
    async def aclose(self) -> None
```

- `HikvisionBackend` wraps the existing `IsapiClient` **unchanged** (search, stream_download,
  clock, semaphore). Its `validate_ref` is the current `ClipRegistry` regex, moved, not
  rewritten.
- `ReolinkBackend` is new (`app/reolink.py`), using `httpx.AsyncClient`:
  - **Session:** login lazily, reuse the token until 5 min before lease expiry, re-login once
    on an "please login first" error code, **logout in `aclose()`** (the lifespan shutdown).
    One login mutex. Reolink cameras allow only a few sessions; leaking a token holds one
    for up to an hour (reolink_aio documents this).
  - **Search:** convert the UTC window to camera wall clock with a measured `DvrClock`
    (same "auto" idea as the DVR: `GetTime` wall clock − our UTC, re-measured every 5 min),
    **split into per-camera-local-day spans** (midnight quirk), search `main` and `sub` per
    span (both can go in one batched JSON array request), drop degenerate `start == end`
    files, pair each main with the sub of greatest overlap (same rule as
    `_substream_for`, ±30 s pad).
  - **Download/stream:** `GET cmd=Download&source=<name>&output=clip.mp4&token=…`, forwarding
    the client's `Range` header; relay status (200/206), `Content-Length`, `Content-Range`;
    force `Content-Type: video/mp4` (firmware sends `apolication/octet-stream`).
  - Own `asyncio.Semaphore` (per device, `max_concurrent_downloads`); token never leaves
    the add-on (it is in the camera URL only, server-side).
  - `validate_ref`: `fullmatch(r"Mp4Record/\d{4}-\d{2}-\d{2}/Rec[MS][0-9A-Za-z]{2}_(?:DST)?\d{8}_\d{6}_\d{6}_[0-9A-Za-z_]+\.mp4")`.
    No `..`, `/` beyond the fixed shape, `&`, `#`, `%`, whitespace. Passed to httpx as a
    query **param** (encoded by httpx), never string-concatenated.

### 3.4 Registry

`ClipRegistry` stores `(Clip, backend)` instead of `Recording`, and calls
`backend.validate_ref()` on `main_ref` and `sub_ref` in `put()`. Clip ids stay opaque, so the
trust boundary is unchanged: the browser can still only name ids minted from real search
results. The `dvr_host` constructor arg goes away (validation moves to the backend).

### 3.5 Endpoints

| Endpoint | DVR clip | Reolink clip |
|---|---|---|
| `GET /api/recordings?camera=<key>\|all&start&end` | paged search (§3.6) | per-day search |
| `GET /api/stream/{id}?quality` | unchanged (stage + remux) | **Range-passthrough proxy**, `video/mp4`, no staging |
| `GET /api/stream-raw/{id}?quality` | unchanged | 400 — not applicable (frontend never calls it) |
| `GET /api/download/{id}?quality=hd\|sd&raw` | now quality-aware (§3.8) | proxy with `Content-Disposition` |
| `GET /api/thumbnail/{id}` | substream when paired (§3.8) | 2 MB prefix of sub file → last frame |
| `GET /api/health` | per-device list | — |

Each clip in `/api/recordings` gains `camera_key`, `camera_name`, `kind`, and
`playback: "remux" | "direct"`. The frontend uses `direct` to skip ffmpeg.wasm entirely and
point `<video>` at `api/stream/{id}`.

`/api/health` keeps `dvr`, `clock_*`, `version`, `client_remux_max_mb` (old frontends) and
adds `devices: [{key_prefix, kind, name, state, clock_offset_s, clock_source}]`.

All-cameras search runs per-camera searches **concurrently** (`asyncio.gather`,
`return_exceptions=True`), merges newest first, and returns partial results plus
`errors: [{camera_key, detail}]` instead of failing the whole search when one device is
down. `truncated` becomes per camera: `truncated_cameras: [key…]` (plus the old boolean).

### 3.6 Paging (Hikvision)

`IsapiClient.search()` gains a loop over `searchResultPosition`: request pages of
`max_results`, continue while `responseStatusStrg == "MORE"` (Hikvision's documented
marker), stop on `OK`/`NO MATCHES` or a short page, with a hard cap
`max_search_results` (new option, default 500, range 40–2000) to bound DVR load. The
`<searchID>` is reused across pages of one search (it is the paging handle — the existing
comment says so). If the cap is hit, `truncated` is true and the UI says so.
**Unverified:** the paging field behaviour on this DVR (README lists it as still
unverified) — `verify_dvr_contract.py` gets a paging check before this ships.

### 3.7 Substream pairing at search time (Hikvision)

Today SD playback does one extra DVR search **per play** (`_substream_for`). Instead, the
list search also searches track `channel+1` for the same window once and pairs by overlap
(same function, moved into the backend). Cost: one extra search per list query instead of
one per play. If the sub search fails, `sub_ref = None` and SD falls back to main exactly as
now. The firmware mislabel (`<trackID>` says 101 for a 102 search) stays ignored.

### 3.8 SD/HD download and substream thumbnails

- `/api/download/{id}?quality=hd|sd`, **default `hd`** (today's behaviour for any caller that
  omits it). The player's Download button follows the SD/HD picker.
- Thumbnails use `sub_ref` when present, else main. The README's reason for main-stream
  thumbnails ("matches what Download gives you") no longer holds once Download is
  quality-aware; the README section is rewritten, not just the code.
- Reolink thumbnails: read the first 2 MB of the sub file (≈16 s at 1 Mbps; for MP4 with
  `moov` up front this includes the index), decode frames, keep the last complete one —
  the existing `thumbnail_from_stream` strategy. **Verify** that ffmpeg decodes a truncated
  faststart MP4 prefix (expected yes; the DVR path decodes truncated MPEG-PS). Fallback if
  not: Range-read `moov` + first GOP.

### 3.9 Frontend (vanilla JS, no build step, relative URLs only — Ingress rule)

- **Camera chips** replace the `<select>`: "All" + one chip per camera; multi-select.
  Default "All". Each row shows a camera chip (exists already as `clip-chip`).
- **Timeline** above the list: one lane per selected camera, scaled to the searched range
  (a full day when "Today"/"Yesterday"/date-only), segments drawn from the search results,
  tapping a segment opens that clip. A current-time marker on today. Pure DOM/CSS
  (absolutely positioned divs), no canvas library.
- **Single player panel** (sticky at top on mobile, side panel on wide screens) instead of
  a player per row: prev/next (in list order), autoplay-next toggle (persisted in
  `localStorage`), SD/HD picker, mode badge (`Direct · native MP4` added), Download (follows
  quality). Keyboard: Space play/pause, ←/→ seek 5 s, `j`/`k` prev/next clip, `f`
  fullscreen. Playback logic per clip: `direct` → `api/stream`; `remux` → existing
  fast/compat logic unchanged.
- **Polish:** CSS grid of thumbnail cards ≥ 720 px, list below; 44 px min touch targets;
  `prefers-color-scheme` dark theme via CSS variables; per-camera error banners for
  partial results; skeleton placeholders while searching.
- The existing `tests/frontend/run.py` headless checks and `test_fallback.html` are
  updated for the new DOM.

### 3.10 Version

`0.2.0` in `config.yaml` and `main.py` `VERSION` (asset cache-busting is keyed on it).
README: new "Reolink cameras" section with the verified table above, midnight quirk,
config example; updated SD/HD + thumbnail sections.

## 4. Alternatives considered

- **Restructure config into a `devices:` list.** Cleaner, but breaks every existing install
  (Supervisor won't migrate) — rejected.
- **Use `reolink_aio` as a dependency.** Battle-tested, but pulls aiohttp + the Baichuan
  protocol stack into a small add-on that needs four commands; also its own session/port
  probing. Used as the **contract reference** instead.
- **Reolink FLV/RTMP playback (`/flv?…`).** Needs the raw password in the URL and RTMP
  enabled; MP4 Download with Range is simpler and already browser-native — rejected.
- **Remux Reolink MP4 anyway** for one uniform path. Pointless cost; verified H.264/AAC
  faststart MP4 plays directly.
- **Timeline via a separate `/api/timeline` endpoint.** Same data as a full-day search;
  rendering from search results avoids a second code path.

## 5. Implementation plan (files)

Phase A — backend (tests first, respx-mocked Reolink JSON built from the probe output):
1. `app/config.py` — `ReolinkCamera`, optional DVR, `max_search_results`. `tests/test_config.py`.
2. `app/backends.py` — `Clip`, `Backend` protocol, `HikvisionBackend` (wrap `IsapiClient`),
   pairing helper moved from `main.py`.
3. `app/isapi.py` — search paging loop. `tests/test_isapi_search.py` (+ paging cases).
4. `app/reolink.py` — `ReolinkBackend`. New `tests/test_reolink.py`: login/lease/relogin/
   logout-on-close, per-day split across midnight, degenerate filter, main/sub pairing,
   Range passthrough + content-type fix, ref validation (traversal, `&`, `%`, CRLF).
5. `app/registry.py` — store `(Clip, backend)`, backend validation. `tests/test_registry.py`.
6. `app/main.py` — string camera keys, `camera=all` fan-out with partial errors, direct
   proxy for native-MP4 clips, quality-aware download, substream thumbnails, health
   devices list, `channel=` compat. `tests/test_api.py`, `test_substream.py`,
   `test_stream_*` updated.
7. `config.yaml` — schema/options, version 0.2.0.

Phase B — frontend: `www/index.html`, `www/app.js`, `www/style.css`; `tests/frontend/*`.

Phase C — docs: README sections; `scripts/verify_dvr_contract.py` paging check;
`scripts/verify_reolink_contract.py` (already written).

One branch, one PR (owner chose "lite"), commits per phase.

## 6. Testing / verification

- Full pytest suite green (134 pass today with ffmpeg installed).
- Live: Reolink search/stream/download/thumbnail through the running app on this Mac
  (`uvicorn` with a local options file, credentials from secret files), iOS playback of a
  direct clip in the HA Companion app after install, DVR paging via the verify script.

## 7. Risks

- **Reolink session exhaustion** if logout is skipped (crash). Mitigation: token reuse,
  single login mutex, logout on shutdown; a crash leaks at most one session for ≤1 h.
- **Wi-Fi camera throughput**: 5 Mbps main over Wi-Fi; SD default keeps playback ~1 Mbps.
- **Per-day split + clock**: a window near midnight on the camera's clock produces two
  searches; DST transition days (23/25 h) must not drop or duplicate files — dedupe by name.
- Paging behaviour on the DVR is unverified until the probe runs against it.

## 8. Open questions for the owner

1. Is a Reolink-only install (no DVR) worth supporting now, or keep the DVR required?
   (Plan: optional — small cost.)
2. Very busy Reolink days: is there a per-search file cap? Probe again on the busiest day.
3. Hikvision paging: OK to run `verify_dvr_contract.py` against the DVR (192.0.2.56) from
   this Mac? Needs DVR credentials via a secure prompt.

---

## 9. Resolutions after goldfish review (2026-11-14)

Goldfish verdict on the draft: **no-go** (2 blockers, 11 majors, 15 missing decisions).
Additional live probes were run to close the factual gaps; owner decisions recorded.
Where this section conflicts with §1–§8, **this section wins**.

### 9.1 New verified facts

Reolink E1 Pro (second probe):
- **Batched Search does not work** — two Search commands in one request returned 0 files /
  `code 1`. One request per (stream, day). (§3.3 batching claim withdrawn.)
- Busiest day in 14: **149 files** in one response; no cap observed.
- **`DST` filename marker is real**: `RecM0A_DST20261101_003727_…` (no underscore between
  `DST` and the date). Clips recorded under DST carry it.
- Range: `bytes=N-` → 206 correct; `bytes=-N` → 206 correct; **multi-range → 206 with no
  `Content-Range` (malformed)**; past-end → **416** `bytes */<total>`. No content-encoding.
- **Bad token → 401 `text/html`**, missing file → **404 nginx HTML**. Errors never arrive as
  a 200, so status codes are sufficient to detect them.
- Thumbnail **through a pipe from a 2 MB prefix** (the real `thumbnail_from_stream` path)
  works for main (107 frames; last partial NAL logs an error, earlier frames clean) and sub
  (clean). Use the sub file.

Hikvision DVR (`verify_dvr_contract.py --page-size`, new Q5):
- **`searchResultPosition` is ignored.** Same 10 items for positions 0..140, with a reused
  or fresh `searchID`, always `MORE`. Position-based paging would loop on duplicates.
- **Hard cap of 64 items per search** regardless of `maxResults` (100 and 200 → 64, `MORE`).
  Existing latent bug: `truncated = len(found) >= max_results` is never true when
  `max_results > 64`.
- Results are **ascending by start time**. **Time-cursor paging works**: re-search from the
  last item's `startTime` (inclusive), dedupe, stop when status ≠ `MORE` or no new items.
  24 h: page 0 = 50 (`MORE`), page 1 = 29 with 1 dupe (`OK`) → 78 distinct.

### 9.2 Owner decisions

- Prev/next and autoplay go **forward in time** (next = later clip), independent of list order.
- **Two stacked PRs**: PR 1 = design doc + backend (config, backends, Reolink, paging,
  registry, endpoints, SD/HD download, thumbnails, README). PR 2 = frontend.
- **DVR optional** when at least one Reolink camera is configured.

### 9.3 Blockers

- **B1 registry eviction.** Capacity = `max(500, n_cameras × max_search_results)`. Entries are
  inserted **oldest-first** (sort ascending for insertion, return newest-first), so if eviction
  ever happens it drops the oldest clips of the current response, never the newest.
- **B2 paused direct streams hold slots.** Direct (Reolink) streaming **clamps every upstream
  request to an 8 MiB slice**: `bytes=N-` becomes `bytes=N-(N+8MiB-1)` and the response is a
  normal 206 with the true total in `Content-Range`. Browsers re-request the next slice. A
  missing Range header is answered as `bytes=0-…` 206 too (WebKit accepts this). Each request
  is short, so pause/abandon holds a slot for at most one slice. Plus a 15 s idle-read
  watchdog (`asyncio.wait_for` on each chunk) that closes upstream and releases the slot.
  `/api/download` is not clamped (a real download reads continuously) but gets the same
  watchdog.

### 9.4 Majors

- **M1** Upstream status mapping (Reolink Download): 401 → re-login once, retry once, then 401
  "camera rejected credentials"; 404 → 410 "recording no longer on the camera"; 416 → 416
  relayed; other ≥400 or `text/html` on a 2xx → 502. Never relay a non-video body as video.
- **M2** Range allowlist: exactly one of `bytes=A-B`, `bytes=A-`, `bytes=-N` (digits only);
  anything else (multi-range, junk) is treated as no Range. Relayed response headers:
  `Content-Range`, `Content-Length`; set `Accept-Ranges: bytes`, `Content-Type: video/mp4`,
  `Cache-Control: no-store`. Request header `Accept-Encoding: identity`.
- **M3** Token never leaves the backend: Reolink errors raise `DvrError` subclasses with
  fixed messages ("Can't reach the camera <name>."); the URL is built inside the request via
  `params=` and no `HTTPStatusError`/URL text is ever formatted into a message or log.
- **M4** Compat: `/api/channels` returns `{"id": <key>, "key": <key>, "name", "kind"}`. Old
  frontends send `channel=<id>` which is now the key (`dvr-101`) **or** a bare int (mapped to
  `dvr-<int>`). Clips keep `channel_name` and `channel` (the key) alongside the new fields.
- **M5** Per-device **search semaphore of 1**. All-cameras fan-out is concurrent across devices
  and serialised within a device.
- **M6** DST: Reolink converts per clip — local wall time + `timeZone` seconds, minus the DST
  offset hours when the filename carries `DST` (fallback when the name doesn't parse: measured
  offset). Search windows are converted with standard time and padded +1 h, then results are
  filtered to the requested UTC window. DVR: unchanged (one measured offset); on a DST-change
  day part of the timeline is off by 1 h — pre-existing, documented in the README as a known
  limit.
- **M7** Login: after an auth failure the backend refuses to log in again for 300 s (health →
  `auth_failed`, requests → 401 immediately). Login mutex re-checks token validity after
  acquiring. Logout in `aclose()` with a 3 s timeout; lifespan closes backends concurrently.
- **M8** Verified (§9.1).
- **M9** Day spans are `[D 00:00:00, D 23:59:59]` intersected with the padded window; results
  deduped by file name across spans. A clip that crosses midnight is listed under its start day
  (dedupe makes double-listing harmless).
- **M10** Paging = the time cursor of §9.1, page size `min(max_results, 50)`, stop on
  status ≠ `MORE` or zero new items or `max_search_results` reached (then `truncated`).
  `parse_search_response` gains a sibling `parse_search_status()`; `IsapiClient.search`
  keeps its signature and return type (`list[Recording]`) and gains
  `search_all(...) -> tuple[list[Recording], bool]` (recordings, truncated).
  `build_search_body` is unchanged. The old `max_results` option keeps meaning "page size".
- **M11** List-time sub search is padded ±30 s; each main clip is paired with the
  max-overlap sub. Unpaired clips keep `sub_ref=None` and SD falls back to the existing
  per-play `_substream_for` lookup (kept), then to main.

### 9.5 Decisions the goldfish asked for

- **Types** (`app/backends.py`):
  ```python
  @dataclass(frozen=True)
  class Clip: camera_key: str; start: datetime; end: datetime; size_bytes: int
              main_ref: str; sub_ref: str | None; sub_size_bytes: int | None
  @dataclass
  class SearchOutcome: clips: list[Clip]; truncated: bool
  @dataclass
  class Upstream:           # an open byte stream from a device
      status: int                     # 200 or 206
      headers: dict[str, str]         # Content-Length / Content-Range when known
      body: AsyncIterator[bytes]      # MUST be aclose()d; releases the device slot
  ```
  `Backend.search(camera, start_utc, end_utc) -> SearchOutcome`;
  `Backend.open(ref, range_header: str | None, clamp: bool) -> Upstream` — raw device bytes
  only. Remux/stage/thumbnail stay in `main.py` and pick the pipeline from `backend.kind`
  (`dvr` → existing remux/stage paths; `reolink` → direct).
- **Reolink `/api/stream-raw`** → 409 "not needed for this camera"; `/api/download?raw=1` on a
  Reolink clip is ignored (it is already MP4).
- **Health**: `dvr` field = DVR state, or `"not_configured"` in a Reolink-only install; new
  `devices: [{"prefix": "dvr"|"reolink-0", "kind", "name", "state", "clock_offset_s",
  "clock_source"}]`. Reolink ping = `GetTime` with the current token.
- **`/api/recordings`**: `camera=<key>` (repeatable for multi-select), `camera=all`, or legacy
  `channel=`. Response `{"clips": [...newest first], "truncated": bool,
  "truncated_cameras": [key], "errors": [{"camera": key, "detail": str}]}`. If **every**
  requested camera fails, respond with the first error's HTTP status (as today). Unknown
  key → 400. `asyncio.gather(..., return_exceptions=True)` only catches `DvrError`; anything
  else re-raises.
- **Clip JSON**: `id, channel (=key), camera_key, channel_name, camera_name, kind,
  playback ("remux"|"direct"), start, end, duration_s, size_bytes, has_sub`.
- **Config schema (final)**: existing keys unchanged except `dvr_host`, `dvr_username`,
  `dvr_password` become `str?`/`password?`; new `max_search_results: int(40,2000)` default
  500; `reolink_cameras` list as §3.1 with `verify_ssl: "bool?"` (default false — cameras use
  self-signed certs; only relevant when `use_https`). Blank `dvr_host` with no Reolink cameras
  → `ConfigError` (as today); blank `dvr_host` with cameras → INFO log "DVR not configured".
- **Frontend list size**: render the first 200 rows, "Show more" for the rest; the timeline
  draws all segments.
- **Keyboard**: ignored while focus is in an input/select; fullscreen uses
  `video.webkitEnterFullscreen()` when `requestFullscreen` is unavailable (iOS).
- **Tests deliberately changed**: `test_registry.py` (constructor no longer takes `dvr_host`;
  validation via backend), `test_api.py` (`/api/channels` shape gains keys; `channel=101`
  still accepted), `test_substream.py::test_recordings_list_and_thumbnails_stay_on_the_mainstream`
  is **inverted** (thumbnails now use the substream when paired). `create_app(config,
  client=...)` keeps its `client` kwarg for the DVR and gains `backends=` for injection.

### 9.6 Changes after the implementation review (advisor, 2026-11-14)

- **Only open-ended ranges are clamped.** A closed range (`bytes=0-21207749`, the
  Safari/AVFoundation pattern) is forwarded exactly — AVFoundation is strict about
  receiving the range it asked for. The consumer-stall watchdog, not the clamp, is the
  B2 protection; it is mutation-tested (`test_a_stalled_consumer_gives_the_slot_back`
  fails when the watchdog is disabled).
- **The consumer watchdog applies to playback only.** Downloads keep only the camera-side
  read timeout: a browser "save as?" prompt may leave the body unread, and a cut-off
  download is a broken file.
- **Token race:** a 401 only drops the token that failed, so two concurrent failures
  cannot discard a fresh token and leak a session.
- **Supervisor schema checked against source** (`supervisor/apps/options.py`,
  `RE_SCHEMA_ELEMENT` ends in `\??$`; missing `?` keys inside list rows are allowed;
  a top-level list-of-dicts key must exist, which the `reolink_cameras: []` default
  guarantees).
- **Known asymmetry:** when `max_search_results` is hit, the DVR keeps the *oldest*
  clips of the window (the time cursor walks forward) while a Reolink camera keeps the
  *newest*. Documented in the README.
- **Not verified on iOS yet** — the direct Reolink path was exercised with curl only.
  Must be played and seeked on an iPhone (Safari and the HA Companion app) before merge.
