# Hikvision Recordings — Home Assistant Add-on

Browse, play and download recordings **already stored on your Hikvision DVR/NVR**.
This add-on records nothing and stores no video — it reads the DVR's own recording
index over ISAPI and streams existing clips straight through to your browser.

## Install

1. Settings → Add-ons → Add-on Store → ⋮ → Repositories
2. Add `https://github.com/jimmyrelismartinez/ha-hikvision-recordings`
3. Install **DVR Recordings**, open Configuration, fill in:
   - `dvr_host` — DVR IP (e.g. `10.10.11.56`). Must match the IP/hostname the DVR reports in its own playback URIs. For Hikvision, this is typically the DVR's IP address, not a hostname.
   - `dvr_username` / `dvr_password` — a DVR account with playback rights
   - `channels` — one row per camera: `id` (e.g. `101`) and `name` (e.g. `Driveway`)
4. Start. The panel appears in the sidebar as **Recordings**.

> Add-on options are stored by Supervisor in `/data/options.json` in plaintext — standard
> for every HA add-on. Use a limited playback/operator DVR account, not `admin`.

## Notes

### Search defaults and result order (v0.1.7)

The search page opens with **Last hour** pre-selected instead of Today — the common
case (checking who just showed up) needs zero taps now. Results also come back
**newest first**. Note this only reorders what the DVR already returned in one page —
it does not change which clips were selected before the `max_results` cutoff
(`truncated: true`); a full fix for that would need DVR-side paging.

### Two playback paths: browser remux first, server remux as the fallback

Playing a clip tries the **fast path** first and silently drops to the **compatibility
path** if anything is missing or goes wrong. A small badge next to the player says
which one actually ran, so you can tell them apart at a glance (or in a screenshot):

| Badge | Path | What happens |
|---|---|---|
| **Fast · browser remux** | `/api/stream-raw` | The add-on proxies the DVR's raw MPEG-PS through untouched — no ffmpeg on the server, nothing staged — and **ffmpeg.wasm remuxes it to MP4 in your browser**, played from a blob URL. |
| **Compatibility · server remux** | `/api/stream` | The original path: fetch, remux with ffmpeg on the host, stage to RAM, serve with `Content-Length` + Range. Unchanged from v0.1.1/v0.1.3. |

The fast path is skipped **before** anything is downloaded when it could not work:
ffmpeg.wasm missing, or a clip larger than the in-browser cap — **128 MB by default as
of v0.1.7** (doubled from 64 MB; the raw clip and its remuxed copy both sit in the wasm
heap, and a phone will run out of memory rather than raise a catchable error). The cap
is configurable via the `client_remux_max_mb` add-on option (16–512, default 128) — the
server exposes it on `/api/health` and the frontend picks it up automatically on load, so
lowering it on a memory-constrained device (or raising it if your phones can take it)
needs no code change. Any failure at all — core won't load, DVR returns 503, ffmpeg exits
non-zero, output is empty — falls through to the compatibility path. There is no dead
end; the worst case is the behaviour you already had.

> ℹ️ **The very first clip in a fresh browser is slower than this.** The wasm core is
> ~32 MB and is downloaded once, then cached by the browser for every later clip. If
> your first play feels slow, that is the core loading, not the feature failing.

**Measured live on this DVR** (36 s clip, SD substream, Chrome, click → playable,
wasm core already cached):

| Path | DVR fetches | Time to playable | Seeking |
|---|---|---|---|
| Browser remux | 1 | **1.8 – 2.8 s** | ✅ full clip |
| Server remux | 20 | 32.8 – 35.6 s | ❌ |

The win is **not** that the browser remuxes faster than the host — a single server
staging cycle is only ~1.5 s, and remuxing is a `-c copy` stream copy either way. The
win is the number of DVR fetches. `/api/stream` stages a fresh copy **per HTTP
request**, and Chrome answers one `<video>` with ~20 Range requests, so the entire
clip is re-fetched from the DVR and re-remuxed 20 times. The browser path fetches it
exactly once. Because the finished MP4 is fully in memory, it is also genuinely
**seekable**, which the staged path is not.

> ℹ️ That 20x re-fetch is pre-existing behaviour of the compatibility path, not
> something this version introduced. Caching a staged clip across Range requests
> would be the obvious next improvement, but the fallback is deliberately left
> untouched here so it stays a known-good safety net.

#### No `SharedArrayBuffer`, no COOP/COEP headers — and that is deliberate

ffmpeg.wasm's **multi-threaded** core (`@ffmpeg/core-mt`) needs `SharedArrayBuffer`,
which needs the page to be cross-origin isolated, which needs
`Cross-Origin-Opener-Policy` + `Cross-Origin-Embedder-Policy`. **Under Home Assistant
Ingress that is impossible.** Cross-origin isolation is inherited from the top-level
document, HA's own frontend sends neither header (verified live), and the add-on panel
is an iframe inside it — so no header this add-on sets can make `crossOriginIsolated`
true. Forcing COEP onto HA's frontend would break unrelated parts of HA.

This add-on therefore ships the **single-threaded** core (`@ffmpeg/core`), which needs
none of that. Verified 2026-07-27 with `crossOriginIsolated === false` and
`SharedArrayBuffer` undefined: a real 3.7 MB DVR clip loaded the core in 243 ms and
remuxed in 365 ms. Stream copying is I/O, not parallel compute, so threads buy nothing
here anyway. **Do not "upgrade" this to `core-mt`.**

ffmpeg.wasm is **vendored into the image at build time** (see the `Dockerfile`), never
fetched from a CDN — playing a clip never requires the container to have outbound
internet. For a local checkout, run `scripts/fetch-ffmpeg-wasm.sh`; without it the
frontend simply uses the compatibility path.

> Also verified under a simulated Ingress path prefix
> (`/api/hassio_ingress/<token>/`), because the UMD bundle loads its worker
> (`814.ffmpeg.js`) by a path derived from its own script URL — a wrong prefix would
> make `ffmpeg.load()` throw and silently pin every play to the compatibility path.
> It resolves correctly, which is why `vendor/814.ffmpeg.js` must stay beside
> `vendor/ffmpeg.js`.

### If a new version's UI doesn't appear after updating

The SD/HD toggle and the mode badge live **inside a clip's player panel** — tap a row
in the results list to open it. They are not on the collapsed list rows.

If you have tapped a clip and still see only the bare video controls, you are almost
certainly running a **cached copy of an older frontend**. This bit us on v0.1.4:
`api/health` correctly reported the new version (a fresh API call) while the iPhone
kept executing the previous release's `app.js`, so the add-on looked broken when it
had simply never loaded the new UI.

Fixed in v0.1.6 and it should not recur: the HTML/JS/CSS are now served with
`Cache-Control: no-cache` (always revalidate — a cheap 304 when unchanged) and asset
URLs carry the add-on version, so `app.js?v=0.1.6` cannot be answered from a cached
`app.js?v=0.1.4`. The ~32 MB `vendor/` wasm core is still cached hard, since its
content is pinned by version in the `Dockerfile`.

**Escaping a cache from before v0.1.6 may still need one manual nudge**, because the
old response never told the browser to revalidate: in the HA Companion app, Settings →
Companion App → Debugging → **Reset frontend cache**, or open the panel once in a
regular browser.

### SD / HD

Each clip has an **SD / HD** toggle next to the player, defaulting to SD:

- **SD** — the substream (track `channel+1`), roughly half the bytes and the faster choice.
- **HD** — the mainstream (track `channel`), full 1920x1080.

Both `/api/stream` and `/api/stream-raw` accept `?quality=sd|hd`, so either playback
path can serve either quality. Picking HD skips the substream lookup entirely rather
than searching for a result it would discard.

**`/api/download` is not quality-aware** — it always gives you the full mainstream,
whatever you picked for playback.

If you choose SD on a channel that has no substream recorded for that window, playback
**falls back to the mainstream and logs it**, exactly as before — asking for SD is a
speed preference, not a precondition.

### Playback uses the substream by default; download gives you full quality

Hikvision track numbering is `channel*100 + streamType` — 1 = mainstream,
2 = substream, 3 = photo/snapshot (**not supported on this DVR**, `statusCode 4` /
`notSupport`, which is why thumbnails decode a frame out of the video instead).

Because the Range fix means a clip is fetched and remuxed *in full* before playback
can start, the size of the file directly sets how long you wait. So **`/api/stream`
plays the substream** (track `channel+1`, e.g. 102 for channel 101), which this DVR
records independently at 960x480 CBR 3072k alongside the 1920x1080 CBR 6144k
mainstream.

**`/api/download` always fetches the mainstream** — full quality, unchanged. The
recordings list and thumbnails also stay on the mainstream, so what you see in the
list describes what Download will give you.

Measured live on this DVR, same 36 s clip, end to end through the add-on
(DVR fetch + remux + staging):

| Endpoint | Track | Resolution | Size | Time |
|---|---|---|---|---|
| `/api/stream` | 102 (sub) | 960x480 | 13.8 MB | **30.8 s** |
| `/api/download` | 101 (main) | 1920x1080 | 24.1 MB | 75.8 s |

That is **~2.5x faster to start playing**, better than the size ratio alone predicts —
the DVR appears to serve the substream more efficiently, not merely to send less.

If a channel has no substream recording, or a particular window exists only on the
mainstream, playback **falls back to the mainstream** and logs it. That fallback is a
real safety net: some Hikvision devices record no substream at all.

> ⚠️ **FIRMWARE QUIRK — do not "fix" this.** A search on trackID 102 returns results
> whose `<trackID>` element says **101**. The label is wrong; the footage really is the
> substream (confirmed with ffprobe: 960x480). The parser deliberately records the
> channel that was *requested* and ignores that element, so nothing trusts the mislabel.
> Do not add code that reads it.

### Thumbnails

Each row in the results list shows a preview frame, lazy-loaded as the row scrolls
into view.

There is no ISAPI shortcut for this on this hardware. The search response carries
no picture field, and the "channel + 300 = photo track" convention that HikLoad,
hikvision-download-assistant and qb60/hikvision-downloader all use (trackID 103 for
channel 101) is rejected by this firmware with `statusCode 4` / `notSupport`. So a
preview is built the only way available: request the clip through the same
`ContentMgmt/download` call the video path uses, read a bounded slice of it, and have
ffmpeg decode a single frame straight from the raw MPEG-PS — no fragmented-MP4 remux
(that exists only to make a whole clip seekable) and no disk write. The JPEG is
~14 KB at 480px wide.

**The frame is the latest one the cheap read reaches — not frame 0, and not a fixed
timestamp.** A motion-triggered recording begins slightly *before* whatever triggered
it, so frame 0 is usually an empty driveway or an empty porch: a valid thumbnail that
tells you nothing. So ffmpeg decodes every frame in the slice that was read and the
**last complete one** is served.

The budget caps the cost; the footage decides how far into the clip that reaches.
Measured on a real 37 s mainstream clip (6144 kbps):

| Read | Frames decoded | Reaches |
|---|---|---|
| 1 MB | 11 | ~1.4 s |
| **2 MB** (current) | **22** | **~2.7 s** |
| 4 MB | 55 | ~5.5 s |

Substreams get this for free: at half the bitrate the same 2 MB reaches roughly twice
as far in. That self-scaling is why this is a byte budget and not a time target.

Nothing seeks, so nothing can land past the end of a short clip — if the slice yields
only one frame, that frame is both the first and the last and is served as-is. A 1 s
recording still gets a real picture.

> **Reverted in v0.1.5: the fixed 15 s seek from v0.1.4.** Targeting an exact
> timestamp meant reading enough bytes to *cover* it — ~13.5 MB at these bitrates —
> and per-thumbnail time went ~0.5 s → 1.2–2.3 s. Because previews share the DVR's
> `max_concurrent_downloads` budget (default 2), each one held a slot ~4x longer and
> scrolling the list visibly stalled on rows still showing the placeholder. Taking
> the last frame *within* the cheap read keeps the benefit and drops the cost:
> measured 0.72–1.20 s per thumbnail, 1.86 s wall clock for three at once.
>
> Verified on real footage that this is not just frame 0 with extra steps: on an 8 s
> porch clip the `t=0` frame is an empty porch and the served frame shows the person
> at the door.

> ⚠️ **The read budget is measured, not guessed.** Truncating a real clip and decoding
> frame 1: at 128 KB ffmpeg *exits 0 and emits a JPEG* whose top quarter is fine and
> whose remainder is smeared garbage. It does not error. 256 KB was visually complete;
> 512 KB was byte-identical to 1 MB and 2 MB. Because too-small a read fails silently,
> this can only be re-tuned by looking at the image, never by watching return codes.
>
> That trap applies to the FIRST frame of a too-small read. The tail of the 2 MB slice
> does not suffer from it — every frame decoded at 1/2/4 MB came out complete (JPEG
> EOI present) and visually clean, because by 1 MB there are already whole GOPs to
> decode. Incomplete trailing images are dropped rather than served, so a read cut
> mid-picture cannot reach the browser.

**Concurrency caveat:** thumbnails go through the same DVR connection budget as
playback and downloads (`max_concurrent_downloads`), deliberately — a 40-row list
would otherwise swamp the device. That is also why the frontend fetches a preview
only when its row scrolls into view. A busy DVR returns 503 for a thumbnail exactly
as it does for a video request, and that row keeps its placeholder.

Measured against the real DVR: **~0.7–1.2 s per thumbnail** end to end, and 1.86 s
wall clock for three concurrent requests (the third queues behind
`max_concurrent_downloads: 2`). That is close to the ~0.4–0.6 s of the original
frame-0 version, for a meaningfully better frame.

> If the list ever does feel sluggish, the next lever is pointing thumbnails at the
> substream — about half the bytes. They deliberately use the mainstream today so the
> preview matches what Download gives you.

### Playback is staged; download is streamed

**Inline playback (`/api/stream`) stages the clip to RAM first.** Verified live on
2026-07-25: a chunked response with `Accept-Ranges: none` and no `Content-Length`
makes iOS Safari / the HA Companion WKWebView refuse to start `<video>` at all —
the crossed-out "media unsupported" icon — even though the bytes were a perfectly
valid H.264 MP4 (ffprobe-confirmed, and Download of the same clip worked). WebKit
wants a seekable response. So the remuxed clip is written to a temp file and served
with `FileResponse`, which supplies `Content-Length`, `Accept-Ranges: bytes` and
206 range replies. **Seeking/scrubbing works as a result.**

**This does not break the add-on's zero-persistence promise**, which is enforced three ways:
1. `config.yaml` sets `tmpfs: true`, so the container's `/tmp` is a RAM filesystem —
   staged clips never touch the Pi's disk. **Do not remove that key.**
2. The staged file is unlinked by a background task the instant the response finishes.
3. A sweep removes anything a crashed response left behind, so orphans cannot accumulate.

Nothing is ever written to the add-on's persistent `/data`, and nothing survives a restart.

Trade-offs: playback starts once the clip is staged rather than immediately (a few
seconds for a typical 20–60 MB clip), and clips above `max_stage_mb` (default 256 MB)
are refused with a 413 pointing at Download rather than filling RAM.

**Download (`/api/download`) still streams straight through with no staging** — it is
a plain browser download that does not need Range. Verified live: byte-identical
output to the staged path, valid MP4.
- Base image: `3.12-alpine3.22` — common to `aarch64-base-python`, `armv7-base-python`, and
  `amd64-base-python` (verified: manifest pulls HTTP 200 on all three, 2026-07-25). Matches
  the dev `.venv` Python (3.12.3) exactly — no dev/prod runtime skew. Task 10's `build.yaml`
  may use this single tag string for all three arches.
- Install target: HAOS core at https://10.10.10.9:9123; jimmy-ha on :8124 is a HA Container
  with no add-on store (verified by probe 2026-07-24: `/api/hassio/*` → 404 on :8124; HA
  frontend + 401 on `/api/` at :9123).
- Slug collision check + arch confirmation on the :9123 instance is PENDING — owner to verify
  in the HA UI before install (requires human login; not checked programmatically).
- Frontend (`hikvision_recordings/www/`) is a plain single-page app served by the FastAPI
  app — no build step, no framework, no CDN. Every URL it fetches is relative
  (`api/channels`, not `/api/channels`) so it survives Home Assistant's rotating Ingress
  path prefix. Verified offline (unreachable-DVR smoke test + grep for absolute-path URLs);
  live-DVR click-through and the iOS playback check are PENDING — owner to verify per the
  Task 9 Step 4 checklist (camera list, timestamp accuracy against `dvr_time_mode`, playback
  latency, download/seek, empty-range message, and the iOS `Accept-Ranges: none` check).

## Verified DVR behaviour

Verified live against **DVR-THD30B-81-HIK** on 2026-07-25.

**Timestamps: the search endpoint is NOT UTC-honest.** `ContentMgmt/search` timestamps are
the device's own local wall clock wearing a bogus `Z`. Measured head-to-head over the last
40 real minutes, searching for recordings known to exist:

| Offset applied to the query | ch101 | ch301 |
|---|---|---|
| `0` — treat `Z` as true UTC | 0 | 0 |
| `-6h` — the UTC offset declared in `<localTime>` | 0 | 0 |
| **measured against the device's own clock** | **1** | **4** |

> ⚠️ A **wide** search window (say 6 h) numerically spans both frames, so it returns
> matches under either hypothesis and looks like proof. Only a narrow window discriminates.
> This trap produced two wrong conclusions before it was settled.

The add-on therefore **measures** the offset (`device wall clock − our UTC`) rather than
reading the declared one. The declared value reported `-06:00` (base CST) while the clock
actually ran on DST (UTC-5) and was ~12 min adrift — trusting it put every query 47:54 off
target and returned nothing. Measuring absorbs the DST error and the clock drift together,
and is self-correcting: a firmware whose endpoint really is UTC-honest reports a wall clock
equal to UTC, measures ~0, and gets no shift.

`dvr_time_mode` options:
- `auto` *(default, recommended)* — measure the offset against the device clock, re-measured
  every 5 minutes so drift and DST transitions self-correct.
- `utc` — apply no shift. For firmwares whose search endpoint really is UTC-honest.
  **Verified wrong for this model** (returns zero matches).
- `local` — apply the *host's* local UTC offset. Unverified against real hardware.
- `declared` — apply the offset declared in `<localTime>`. This is what the code originally
  did; wrong under DST on this model. Escape hatch only.

`/api/health` reports `clock_offset_s` (what is applied) and `clock_drift_s` (how far the
device's clock sits from a whole-hour zone — a device-clock health signal).

**Still unverified** (deferred, needs a dedicated run of `scripts/verify_dvr_contract.py`):
thumbnails in `searchMatchItem`, HTTP `Range` support on download, and the search paging
field name.
