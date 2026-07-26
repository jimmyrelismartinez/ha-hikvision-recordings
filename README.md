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

### Thumbnails

Each row in the results list shows a preview frame, lazy-loaded as the row scrolls
into view.

There is no ISAPI shortcut for this on this hardware. The search response carries
no picture field, and the "channel + 300 = photo track" convention that HikLoad,
hikvision-download-assistant and qb60/hikvision-downloader all use (trackID 103 for
channel 101) is rejected by this firmware with `statusCode 4` / `notSupport`. So a
preview is built the only way available: request the clip through the same
`ContentMgmt/download` call the video path uses, read **only the first 1 MB**, and
have ffmpeg decode a single frame straight from the raw MPEG-PS — no fragmented-MP4
remux (that exists only to make a whole clip seekable) and no disk write. The JPEG
is ~13 KB at 480px wide.

> ⚠️ **The 1 MB read cap is measured, not guessed, and should not be trimmed.**
> Truncating a real clip and decoding frame 1: at 128 KB ffmpeg *exits 0 and emits a
> JPEG* whose top quarter is fine and whose remainder is smeared garbage. It does not
> error. 256 KB was visually complete; 512 KB was byte-identical to 1 MB and 2 MB.
> Because too-small a read fails silently, this can only be re-tuned by looking at the
> image, never by watching return codes.

**Concurrency caveat:** thumbnails go through the same DVR connection budget as
playback and downloads (`max_concurrent_downloads`), deliberately — a 40-row list
would otherwise swamp the device. That is also why the frontend fetches a preview
only when its row scrolls into view. A busy DVR returns 503 for a thumbnail exactly
as it does for a video request, and that row keeps its placeholder.

Measured against the real DVR: **~0.4–0.6 s per thumbnail** end to end (three
concurrent requests completed in 866 ms wall clock).

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
