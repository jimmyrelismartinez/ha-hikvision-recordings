# Design: camera views redesign — HA snapshots, event types, per-camera timeline (v0.4.0 / v0.5.0)

Status: DRAFT — for goldfish review (Elephant-Goldfish "lite": one doc, one fresh-context review)
Branch: `feat/v0.4-redesign` (backend PR), then a stacked frontend PR
Date: 2026-11-15

## 1. Problem

v0.3.0 is search-first: pick cameras, a date and a time window, press Search, get a lane
timeline and a list. The owner tested it on an iPhone and likes it, but wants the add-on to
present recordings the way the Scrypted NVR demo does (https://demo.scrypted.app/#/):

- **Home**: a strip of recent events with thumbnails, then one tile per camera.
- **Camera page**: a big stage at the top, under it a **vertical timeline** (newest at
  the top), event thumbnails beside a continuous bar, a date chip, and event-type filters.

The owner is open to a "live" picture in the add-on and asked for pushback. Both devices
(the Hikvision DVR and the Reolink E1 Pro) are already set up in Home Assistant.

Non-goals (this design): live video (RTSP/WebRTC relay), PTZ, recording, camera
discovery from HA (a later PR — §9), notifications.

## 2. Verified facts (live probes, 2026-11-15, read-only)

| Question | Answer |
|---|---|
| Hikvision: event type per clip? | **Yes.** Every `searchMatchItem` carries `<metadataMatches><metadataDescriptor>recordType.meta.std-cgi.com/motion</metadataDescriptor></metadataMatches>`. On this DVR **every** clip of a day was `motion` (50/50). |
| Hikvision: filter by type in the search? | Sending `<metadataList>` is fragile: `…/VMD` → **400 Invalid XML Content**; `…/CMR` → no matches; `…/AllEvent` and `…/smart` behave like no filter. **Do not send `metadataList`** — keep the search body exactly as it is; read the type from the response only. |
| Reolink: event type per clip? | **Yes, from the file name.** `RecM0A_20261114_040923_040948_0_9B288080000000_109CF6A.mp4`: the 6th `_` field is a hex flags word. The Search result itself has only `name, size, type(main/sub), StartTime, EndTime, width, height, frameRate`. |
| Reolink flags decoding | Same bit layout HA's Reolink integration uses (library `reolink_aio`, `typings.py`, `FLAGS_CAM_V2`, `decode_hex_to_flags`): reverse the bits of the whole word, then read bit 17 person, 18 face, 19 vehicle, 20 animal, 23 schedule, 24 motion, 26 doorbell. Our camera: version `0A` (10), 7 `_` fields, 14 hex digits. One day of 89 files decoded to: motion 39, person+motion 23, person 13, person+animal+motion 10, animal 2, person+animal 1, animal+motion 1. Plausible for a porch camera. |
| HA snapshots | **Not probed yet** (HA was not reachable from the dev machine by name). Documented contract: an add-on with `homeassistant_api: true` gets `SUPERVISOR_TOKEN` and can `GET http://supervisor/core/api/camera_proxy/<entity_id>` with `Authorization: Bearer <token>` → JPEG. To be probed with a long-lived token before the frontend PR (§8, Q1). |

### 2.1 Correction to what the owner was told

- *"HA snapshots never touch the devices."* Overstated. Whether a snapshot touches the
  device depends on the HA entity. The Reolink integration uses the camera's snapshot
  API (cheap). A Hikvision entity set up via Generic Camera/ONVIF **without** a still-image
  URL makes HA run ffmpeg against RTSP for every snapshot — a real stream session on the
  DVR. Hence the slow refresh and server-side cache in §3.3.
- *"Thumbnail disk cache."* Wrong for this add-on: it has a zero-persistence guarantee
  (`config.yaml`: staging lives in RAM via tmpfs). The cache is **in memory** (§3.5).

## 3. Backend (PR A — v0.4.0)

### 3.1 Config

Add to `config.yaml`:

```yaml
homeassistant_api: true          # top level: gives the add-on SUPERVISOR_TOKEN
schema:
  channels:
    - id: int
      name: str
      ha_camera: str?            # new, optional
  reolink_cameras:
    - name: str
      # …existing keys…
      ha_camera: str?            # new, optional
```

No change to `options:` defaults (Supervisor does not migrate options; an absent key means
"not linked"). `ha_camera` is an HA entity id such as `camera.porch`.

**Validation (security boundary).** The value is pasted into a URL that carries the
Supervisor token. `config.py` must reject anything not matching `^camera\.[a-z0-9_]+$`
with a `ConfigError` naming the row (e.g. `channels[0].ha_camera`). Without this,
`camera.x/../states` would make the add-on a proxy to the whole HA API. Empty string ==
absent. Store it on `Channel` / `ReolinkCamera` as `ha_camera: str | None`.

### 3.2 HA client — `app/ha.py` (new)

```python
class HaClient:
    base = "http://supervisor/core/api"
    def __init__(self, token: str | None, ttl_s=2.0, timeout_s=10.0, max_bytes=5 MiB)
    @property enabled -> bool          # token present
    async def snapshot(self, entity_id: str) -> tuple[bytes, str]  # (jpeg, content_type)
    async def aclose()
```

- Token from `os.environ.get("SUPERVISOR_TOKEN")`. Absent (local dev, tests) → `enabled`
  is false and the feature is off cleanly; nothing else changes.
- **Never** send the token to the browser, never log it, never put it in an error detail.
- Re-validate `entity_id` with the same regex before building the URL (defence in depth).
- **Per-entity cache, TTL 2 s, single-flight**: concurrent requests for the same entity
  share one upstream fetch; within TTL the cached bytes are returned. Many open phones
  therefore cost HA (and possibly the DVR) at most one snapshot per entity per 2 s.
- Reject a response that is not `image/*` or exceeds `max_bytes` (stream and stop).
- Errors map to the existing `DvrError` family style: HA unreachable/5xx/timeout → 502
  "Home Assistant did not return a snapshot."; 401/403 → 502 with a log line saying the
  add-on lacks `homeassistant_api` (no token text); 404 → 404 "Home Assistant has no
  camera `camera.x`." (the entity id is config, not secret).

### 3.3 Endpoint — `GET /api/snapshot/{camera_key}`

- Unknown camera key → 404. Camera has no `ha_camera` → 404 "Not linked to a Home
  Assistant camera". HA client disabled → 503 "Snapshots need the Home Assistant API
  (homeassistant_api)".
- 200 `image/jpeg` (or whatever image type HA sent), `Cache-Control: no-store`.
- `/api/channels` adds `"snapshot": true|false` per camera (linked **and** client enabled).
- `/api/health` adds `"ha_api": "ok" | "off"` (`off` = no token). No upstream call.

### 3.4 Event types — one normalized field

`Clip` gets `events: tuple[str, ...] = ()`. `_clip_json` adds `"events": [...]`.
Vocabulary (closed set): `motion`, `person`, `vehicle`, `animal`, `face`, `doorbell`,
`scheduled`, `alarm`. Order in the list: the order above.

- **Hikvision** (`isapi.parse_search_response`): for each item, read every
  `metadataMatches/metadataDescriptor`; take the text after the last `/`, lowercase, map:
  `motion`/`vmd` → `motion`; `cmr`/`timing`/`continuous` → `scheduled`;
  `alarm`/`alarminput` → `alarm`; `pir` → `motion`; anything else → dropped (logged at
  debug). The search request body is **unchanged**.
- **Reolink** (new `reolink_flags.py`, ~40 lines, own code with a comment citing
  `reolink_aio/typings.py` as the source of the bit positions — not a dependency):
  decode only when `(version, number_of_fields, hex_length)` is in a known table —
  versions 2, 3, 4 (6 fields, 7/7/9 hex), 7 (7 fields, 10 hex), 9, 10 (7 fields, 14 hex) —
  and return `()` for anything else. Do **not** fall back to "newest known version" the
  way reolink_aio does: on new firmware that yields wrong bits, and a wrong "person" label
  is worse than no label. Hub (9-field) names → `()`.
  Mapping: bit17 person, 18 face, 19 vehicle, 20 animal, 23 scheduled, 24 motion,
  26 doorbell. The main and sub files of a clip are decoded from the **main** name.
- Unit tests: the seven real flag words in §2 with their expected sets; an unknown
  version → `()`; a malformed hex → `()`; DST name variant.

### 3.5 Thumbnail cache — in memory

`/api/thumbnail/{clip_id}` keeps working as now, but first looks up an in-memory LRU:

- Key: `(camera_key, start_iso, ref_used)` — **not** the clip id, which is minted fresh on
  every search. `ref_used` is the sub or main ref actually read, so a Reolink file that the
  SD card later overwrites gets a different key.
- Bounds: 32 MiB total **and** 1000 entries, whichever comes first; evict LRU.
- Only successful JPEGs are cached; errors are not.
- The 503/502 behaviour and the shared download budget are unchanged on a miss.
- `Cache-Control` unchanged.

### 3.6 Recordings endpoint

Unchanged except `events` in each clip. No server-side event filter: per-camera day
lists are complete, so the browser filters.

## 4. Frontend (PR B — v0.5.0)

Constraints that do not change: vanilla JS, no build step, no CDN, **every URL relative**
(HA Ingress), localStorage wrapped in try/catch, iOS `playsinline`, **one `<video>`
element for the whole app** (reusing it keeps the iOS autoplay grant; detaching a
fullscreen element exits fullscreen), keyboard shortcuts as in v0.3.0, light/dark themes.
Additional plain script files are fine (`<script src="views.js">` before/after `app.js`).

### 4.1 Routes (hash only)

`#/` home · `#/camera/<key>` camera page · `#/all` the v0.3.0 search page.
Hash routes only — `pushState` paths break on reload under Ingress. A header tab bar:
**Cameras** (`#/`) and **All recordings** (`#/all`). Back from a camera page returns to
`#/`. Unknown hash → `#/`.

### 4.2 Home `#/`

1. **Recent** strip: horizontally scrolling cards (thumbnail, camera name, time, event
   badges), the newest 12 clips across all cameras from one
   `api/recordings?camera=all&start=now-6h&end=now`. Empty → "No recordings in the last
   6 hours". A "See all" link → `#/all`. Tapping a card → `#/camera/<key>` with that clip
   playing.
2. **Camera tiles**: grid (1 column < 600px, 2 ≥ 600px, 3 ≥ 1000px). Each tile: the HA
   snapshot (when `snapshot: true`) or the newest clip thumbnail, otherwise a plain
   placeholder; camera name; "last clip 12 min ago" (from the Recent search; "—" if none in
   the window); a small error dot when `/api/health` reports the device down.
   Snapshot refresh: every **60 s**, only while `document.visibilityState === 'visible'`
   and the home view is shown; cache-busting `?t=` query. A failed snapshot keeps the
   previous image and falls back to the thumbnail after two failures.
   Tap a tile → `#/camera/<key>`.

### 4.3 Camera page `#/camera/<key>`

- **Stage** (the shared `#player-panel`, always visible on this page): when no clip is
  playing it shows the HA snapshot labelled **"Live · snapshot"**, refreshed every **3 s**
  while the page and tab are visible (server cache makes this ≤ 1 upstream fetch per 2 s
  per camera regardless of viewers). No snapshot → shows the newest clip's thumbnail with
  "Latest recording". When a clip plays, the same stage shows the video, quality toggle,
  Download, Earlier/Later, Autoplay (all v0.3.0 behaviour, scoped to **this camera**). A
  "● Live" button returns to the snapshot (pauses and releases the clip).
- **Date chip**: sticky at the top of the timeline, shows the day currently scrolled into
  view ("Today", "Yesterday", "Thu 12 Nov"). Tapping it opens a native date input; picking
  a date loads that day and scrolls to it.
- **Event filters**: chips All · Person · Vehicle · Animal · Motion — only the types that
  occur in the loaded days are shown; if only one type occurs (the DVR: all `motion`),
  the chip row is hidden. Filtered-out clips stay on the bar as dimmed segments but their
  cards are hidden. Persisted per camera in localStorage.
- **Vertical timeline**, newest at the top, one section per day (camera's day as seen by
  the browser's local time):
  - Layout unit is the clip. Each clip is a row: on the left a time label, in the middle a
    segment on a continuous vertical bar, on the right a card (thumbnail, start time,
    duration, event badges). Row height = `max(card height, duration_s × 2 px)`.
  - Gaps between clips are drawn as bar-less spacers: proportional (`gap_s × 2 px`) up to
    10 min, otherwise a fixed 28 px band labelled with the gap ("2 h 13 m"). This keeps a
    quiet night from being a long empty scroll.
  - Hour ticks on the left where an hour boundary falls inside a row or a proportional gap.
  - A "now" line at the top of Today.
  - **Tap-to-seek**: tapping the segment at height *y* of a row opens that clip and seeks to
    `y / rowHeight × duration` once metadata has loaded. Tapping the card plays from 0.
  - Playing clip is highlighted (`.active`, `aria-current`) and scrolled into view when
    Earlier/Later/Autoplay change it.
- **Infinite scroll**: the page loads Today (00:00 local → now) for this camera
  (`camera=<key>`). An IntersectionObserver sentinel at the bottom loads the previous day.
  Stop after **7 consecutive empty days** or **30 days** back, with "No older recordings".
  A failed day shows an inline retry row; it does not stop later loads. Only one day loads
  at a time. Thumbnails use `loading="lazy"`.
- Earlier/Later and Autoplay walk this camera's loaded clips forward in time, crossing day
  sections; reaching the oldest loaded clip with Earlier triggers loading the previous day.

### 4.4 All recordings `#/all`

The v0.3.0 page, unchanged in behaviour (chips, form, lane timeline, list, one player),
plus event badges on rows and timeline segment titles. Its v0.3.0 browser tests must keep
passing — they may be adapted only to navigate to `#/all` first.

### 4.5 "Open live in Home Assistant"

**Open question (§8 Q2).** From inside the Ingress iframe there may be no stable deep link
to a camera's more-info dialog. Until verified, the camera page offers only the near-live
snapshot (§4.3). If a stable URL is confirmed, add a "Open in Home Assistant" button
(`target="_top"`) — never guess a URL.

### 4.6 Load on the devices (budget)

Per open phone, steady state: home = 1 snapshot/min/linked camera; camera page = 1
snapshot/3 s for one camera (server-capped to 1/2 s); thumbnails are lazy and cached in
RAM server-side and by the browser. No polling of `/api/recordings` — a pull-to-refresh /
"Refresh" button on Today re-fetches Today only.

## 5. Alternatives considered

- **Real live video in the add-on** (RTSP → WebRTC/HLS relay). Rejected: heavy CPU on the
  Pi, duplicates what HA already does, and uses the DVR/Reolink connection slots that
  playback needs.
- **Polling HA's MJPEG `camera_proxy_stream`.** Rejected for now: for most entities HA
  emulates it by polling stills anyway, and a long-lived stream per viewer through the
  add-on is harder to bound than the 3 s snapshot.
- **Disk thumbnail cache.** Rejected: zero-persistence guarantee.
- **Server-side event filter / ISAPI `metadataList`.** Rejected: firmware rejects some
  descriptors (400), and the browser already holds complete day lists.
- **Depending on `reolink_aio`.** Rejected: large async dependency for 40 lines of bit
  reading; and its fallback-to-newest-version is the wrong failure mode for labels.

## 6. Implementation plan (files)

PR A (backend, v0.4.0):
- `hikvision_recordings/config.yaml` — `homeassistant_api: true`; `ha_camera: str?` on both
  lists; version `0.4.0`.
- `app/config.py` — `ha_camera` field + regex validation.
- `app/ha.py` — new (§3.2). `app/reolink_flags.py` — new (§3.4).
- `app/clips.py` — `events` on `Clip` (preserved by `with_sub`).
- `app/isapi.py` — parse `metadataMatches` into `Recording.events`.
- `app/reolink.py` — decode events from the main file name.
- `app/thumbnail.py` or `app/main.py` — in-memory LRU (§3.5).
- `app/main.py` — `/api/snapshot/{key}`, `snapshot` in `/api/channels`, `ha_api` in
  `/api/health`, `events` in clip JSON, `VERSION = "0.4.0"`.
- Tests: `tests/test_ha.py`, `tests/test_reolink_flags.py`, additions to
  `test_config.py`, `test_isapi_search.py`, `test_api.py`, `test_thumbnail.py`.
- README: `ha_camera` and the permission note.

PR B (frontend, v0.5.0): `www/index.html`, `www/app.js` (+ optional new plain script files),
`www/style.css`, `tests/frontend/test_fallback.html` (+ cases), README section, version
`0.5.0` in `config.yaml` and `main.py`.

## 7. Testing / verification

- pytest: everything that passes today keeps passing (213 passed, 1 skipped baseline),
  plus the new tests. Fixtures use placeholder names (Porch, Driveway, Garden) and
  `192.0.2.x` addresses only.
- HA client tests with `httpx.MockTransport`: token header present, token absent →
  disabled, regex rejection, single-flight (two concurrent calls → one upstream request),
  TTL expiry, non-image rejection, size cap, 404/401/5xx mapping, token never in a
  response body or log record.
- Browser harness (`tests/frontend/run.py`, headless Chrome): all v0.3.0 cases pass on
  `#/all`; new cases for routing, home tiles (snapshot vs thumbnail fallback), recent
  strip, camera page day load, infinite scroll (stubbed days, stop rule), date chip, event
  filters, tap-to-seek offset, Earlier/Later across days, "● Live" returns to snapshot,
  single `<video>` across views, snapshot polling pauses when hidden.
- Manual on the owner's iPhone in HA after each merge.

## 8. Open questions for the owner / probes before PR B

- **Q1 (probe, before PR B):** HA `camera_proxy` latency and which entity ids exist — needs
  an HA long-lived token (via the secret card) and HA's LAN URL.
- **Q2:** a stable link from the add-on to HA's camera view — to be probed in the real HA;
  until then not built.
- **Q3:** Recent window of 6 h and the 12-card strip — defaults, easy to change.

## 9. Later PR — discovery from HA (not this design)

List HA `camera.*` entities (`/api/states`) and offer them for `ha_camera`; pre-fill host
and name from the device registry where HA has them. Credentials stay user-entered.

## 10. Resolutions after goldfish review (2026-11-15)

The review was checked against the code; every cited line held. **These resolutions
override §3–§7 wherever they differ.** Decisions taken by the design owner (Claude, per the
owner's delegation); owner confirmed the scope earlier (redesign now, discovery later) and
accepted `homeassistant_api`.

### 10.1 Blocker — clip ids under infinite scroll (PR A)

`ClipRegistry.put` mints a fresh id per search and FIFO-evicts; a long scroll would 410
Today's clips. Fix in `registry.py`: keep a secondary index
`(kind, camera_key, start, main_ref) → clip_id`. `put` for an already-known clip **returns
the existing id**, refreshes its TTL, moves it to the MRU end, and updates its `Clip`
(e.g. a newly paired sub). Raise default capacity to `max(2000, …)`. Tests: same clip
twice → same id; TTL refreshed; eviction removes the index entry too.
PR B: on HTTP 410 from stream/thumbnail/download, re-fetch that clip's day and retry once
by `clipKey`; a second 410 shows "This clip is no longer available — refresh".

### 10.2 Security (PR A)

- **Token isolation.** At startup read `SUPERVISOR_TOKEN` once, then `os.environ.pop` it.
  Every subprocess (`thumbnail.py`, `remux.py`) gets an explicit minimal
  `env={"PATH": …, "LANG": "C.UTF-8"}` — ffmpeg parses untrusted device bytes and must not
  inherit the token. Test: the env passed to `create_subprocess_exec` has no
  `SUPERVISOR_TOKEN`.
- `HaClient` has its own `httpx.AsyncClient(follow_redirects=False, trust_env=False)`.
- Entity id: `re.fullmatch(r"camera\.[a-z0-9_]{1,64}", value)`; test `"camera.x\n"`,
  `"camera.x/../states"`, `"light.x"`, uppercase → rejected. Schema:
  `ha_camera: "match(^camera\\.[a-z0-9_]{1,64}$)?"` on both lists.
- Snapshot responses: only `image/jpeg` or `image/png` pass; anything else → 502.
  Always send `X-Content-Type-Options: nosniff` and `Cache-Control: no-store`.
- **Ingress-only listener**: a middleware that, when running under Supervisor (token
  was present), rejects requests whose peer is not `172.30.32.2` (the Supervisor ingress
  proxy) or `127.0.0.1` with 403. Off when no token (local dev/tests). Test both modes.

### 10.3 Snapshot load (PR A + B)

- Server: single-flight per entity runs in a **shielded task** (one client disconnecting
  does not cancel the shared fetch). **Failures are cached for the TTL** too. TTL 2 s.
- Client: never a fixed interval — schedule the next fetch only after the previous image
  loaded or failed. Camera page: every **5 s** (not 3 s). Home tiles: every 60 s. Both stop
  when the view/tab is hidden **and** after **2 min without interaction**, showing
  "Paused · tap to resume". Final cadence is revisited after Q1.

### 10.4 Thumbnails (PR A + B)

- Server: the LRU (§3.5) also single-flights per key, so two viewers of the same row cost
  one device read.
- Client: reuse the existing `thumbObserver` (app.js), at most **2 thumbnail requests in
  flight** app-wide, queued newest-visible first; a row that leaves the viewport before
  its request starts is dropped from the queue. No `loading="lazy"` reliance.

### 10.5 Timeline simplified (PR B) — replaces the geometry in §4.3

Cut for v0.5.0: proportional row heights, hour ticks, tap-to-seek (the native scrubber
seeks). Keep the Scrypted look with simple geometry:
- Each clip is a fixed-height row (the card height): time label left, a bar segment in the
  middle column (the bar is continuous through the rows of a run of clips), card right.
- Between clips with a gap ≥ 2 min: a 28 px band with no bar, labelled "2 h 13 m".
- Newest at the top; a "now" line at the top of Today.

### 10.6 Day loading (PR B)

- **Dedupe by `clipKey`** across days (a clip spanning midnight comes back for both
  days); it is shown in the day of its start.
- A failed day shows a retry row and **pauses** auto-loading until the retry succeeds.
- Auto-load (sentinel) only while the last loaded day added at least one *visible* card or
  is empty; when a filter hides everything, show a "Load earlier day" button instead.
- Stop rule unchanged: 7 consecutive empty days or 30 days back → "No older recordings".
- `truncated` for a day → inline notice "Not every clip of this day could be listed".

### 10.7 Recent strip and routes (PR B)

- Times sent to the API are ISO-8601 UTC (`toISOString()`), never relative strings.
- Recent is fetched on entering `#/` only if the last fetch is older than 60 s.
- Tapping a Recent card calls `openClip` **synchronously in the tap handler** (iOS
  gesture), then sets `#/camera/<key>?t=<start ISO>`; the camera page loads the day
  containing `t`, reconciles the playing clip by `clipKey`, and scrolls to it. The route
  `?t=` with no playing clip just loads that day and scrolls to it.
- Leaving a route disposes the playing clip (generation bump, abort). `#player-panel` is
  never re-parented; views are siblings and layout is switched with a class on `<body>`.
- Health dot mapping: a camera key `dvr-*` maps to device prefix `dvr`; a Reolink key maps
  to its own prefix (`devices[].prefix` equals the camera key).

### 10.8 Event labels (PR A)

- Reolink: decode **only** basenames with 7 `_` fields, version (hex, chars 4–5 of the
  first field) `09` or `0A`, flags = **6th field, 14 hex digits**; everything else `()`.
  "Bit n" is counted after reversing the bit string of the whole word (so bit 0 is the
  word's least significant bit's mirror — follow `decode_hex_to_flags` exactly).
  Test vectors (from the live probe):

  | flags word | events |
  |---|---|
  | `9B288080000000` | `motion` |
  | `9B28C080000000` | `motion, person` |
  | `9B28C000000000` | `person` |
  | `9B28C880000000` | `motion, person, animal` |
  | `9B288800000000` | `animal` |
  | `9B28C800000000` | `person, animal` |
  | `9B288880000000` | `motion, animal` |

  (Output order follows §3.4: motion, person, vehicle, animal, face, doorbell, scheduled,
  alarm.)
- `Recording.events` defaults to `()`; `_as_recording` keeps working unchanged.
- Filters (PR B): chips for every vocabulary type that occurs in the loaded days; hidden
  when ≤ 1 type occurs. Earlier/Later/Autoplay **skip** filtered-out clips. A persisted
  filter whose type is absent falls back to All.

### 10.9 Static files (PR B)

Any new script file must be added to the version-stamped list in `main.py` `index()` and to
`tests/test_static_cache.py` (stale WKWebView cache, see `main.py`).

### 10.10 Release

PR A (v0.4.0) is merged first and **is the Q1 probe**: the owner sets `ha_camera` on one
camera, updates the add-on, and opens `api/snapshot/<key>` through the add-on. No HA token
leaves HA. PR B is built on top meanwhile and its snapshot cadence is adjusted if the probe
shows slow (ffmpeg-backed) snapshots.
