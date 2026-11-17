# External playback: short-lived signed clip links (issue #5)

Status: approved; goldfish resolutions in §12. **Precedence: §12 > §11 > §1–§10.**
Snapshots (`kind: snapshot`) are out of scope everywhere (§11.3), despite older text below.
Scope: this repo = add-on side. HA rest_command/script/automation and the frame
firewall rule live in the homelab repo (Jimmy).

## 1. Problem
HA automations ("motion on Driveway → play the latest clip on the frame") need to
hand another LAN device a URL for a clip or snapshot. The add-on is ingress-only
(needs an HA session), and 8099 is closed to the LAN. Goal: short-lived, signed,
resource-bound URLs, with no open media and no credentials on the client.

## 2. Shape
Two steps, two security boundaries:

1. **Mint (HA core → add-on, internal network)** — `POST /internal/mint` on 8099.
   - Auth: `Authorization: Bearer <mint token>`. The token is a new `password`-type
     option `external_mint_token`, compared in constant time, and stored in HA via
     `!secret`. A peer-IP check alone is not enough: HA core runs on the host
     network, so its requests come from the hassio bridge gateway, and so do other
     host-network add-ons and host processes.
   - Only this exact path is exempt from `IngressOnlyMiddleware`; every other
     8099 path keeps today's ingress-only rule.
   - Request JSON: `{kind: "clip"|"snapshot", camera: "<key>", clip_id?, event?:
     "person"|"motion"|…, since?: ISO, wait_s?: 0-90, quality?: "sd"|"hd"}`.
   - Response: `{url, expires_at, camera, start, duration_s, events, content_type}`.
     The URL is relative to the configured external base (§5), or absolute when
     `external_base_url` is set.
2. **Fetch (client → add-on)** — a **separate ASGI app** on a second port, holding
   only `GET/HEAD /m/{token}`. Everything else returns 404. There is no ingress
   UI, no /api, no CORS. It starts only when the option is on.

## 3. Token
- `token = b64url(payload) "." b64url(HMAC-SHA256(key, payload))`
- `payload = {k: kind, c: camera, i: clip_id|null, q: quality, x: exp_unix}`
- The key is 32 random bytes generated at startup and kept only in memory, so a
  restart invalidates every link. That is fine for short-lived links.
- TTL: option `external_link_ttl_s`, default 300, cap 900.
- Reusable within the TTL, because ExoPlayer issues several Range requests.
- An altered payload, a mismatched resource, or an expired link → 403. An unknown
  or evicted clip → 410.
- Token paths are never logged (access log is already off; add a test).

## 4. "Latest clip after motion" (the trap)
A motion trigger fires while the clip is still recording. Both devices list a clip
only after it closes, so a naive "latest" returns the previous clip.
- `since` = the trigger time. Mint returns the newest clip with `start >= since - 30 s`
  (matching `event` if given).
- `wait_s` (≤ 90): the server polls the device search every 5 s until such a clip
  appears. If none appears, it returns 404 `{"detail": "not_ready"}`. The HA
  example sets `rest_command` `timeout:` above `wait_s`.
- Without `since`, it returns the newest closed clip in the last 24 h.
- Real indexing delay on the DVR and the Reolink is unknown; Jimmy measures it in
  the on-device step.

## 5. Serving clips without re-downloading per Range request (the big gap)
`/api/stream` stages the whole clip and deletes it after each response
(`_discard_stage`). ExoPlayer makes several Range requests per playback; each one
would re-pull and re-remux the clip from the DVR and compete for
`max_concurrent_downloads`.
- New **link stage cache** keyed by (clip_id, quality):
  - It is the same RAM tmpfs `/tmp` staging and counts against `max_stage_mb`.
  - Entries are kept until the last link pointing at them expires, then unlinked;
    a periodic sweep is the backstop. Zero persistence still holds: RAM only,
    nothing in `/data`, and nothing survives a restart.
  - Single-flight, using PR A's done-callback pattern.
  - **Staging starts at mint time**, so the frame doesn't sit on a spinner and the
    fetch is a plain `FileResponse` with Range support.
- Format: Hikvision clips are **fragmented MP4** (`-movflags frag_keyframe+empty_moov`,
  H.264 stream copy; audio as recorded). ExoPlayer plays fMP4. The issue's "moov at
  front" is inaccurate; the docs will say fMP4.
- Reolink clips are already MP4: proxied with Range passthrough, as `/api/stream`
  does (clamped slices), no staging.
- Snapshot: proxies `ha.snapshot(entity)` (already cached and single-flight) and
  returns image/jpeg. See §9 Q3 for a possible scope cut.

## 6. Process / config
- `run.sh`: one Python process starts two `uvicorn.Server`s: 8099 always, and the
  link port only when `external_links: true`. They share the registry, DVR clients,
  `HaClient` and the stage cache. `SUPERVISOR_TOKEN` is still popped once.
- `config.yaml`:
  - `ports: {"8100/tcp": null}` keeps the port unmapped until the user sets a host
    port in the add-on Network tab. Verify that `null` behaviour against the
    Supervisor docs.
  - New options: `external_links: false`, `external_mint_token: ""` (password),
    `external_link_ttl_s: 300`, `external_base_url: ""` (e.g.
    `http://192.0.2.10:8100`).
  - The add-on refuses to start the listener if the token is empty or shorter than
    24 characters.
- Version 0.7.0.

## 7. Docs (README)
- Enabling: the option, setting the host port, and the firewall note (frame → HA
  host:port). Real addresses stay in homelab only.
- HA example: `rest_command` (URL `http://<addon-hostname>:8099/internal/mint`,
  bearer from `!secret`, `timeout: 100`, `response_variable`) → script → call the
  frame's play script. The add-on hostname comes from `ha addons info` → `hostname`.

## 8. Testing
- Unit tests:
  - signing round trip; tamper; wrong resource; expiry; TTL cap;
  - constant-time compare used;
  - token never appears in logs.
- Middleware with Supervisor mode ON:
  - `/internal/mint` reachable only with a valid bearer;
  - every other path is still 403 for non-ingress peers;
  - ingress behaviour unchanged.
- Link app:
  - only `/m/*`; 404 elsewhere; HEAD works;
  - Range returns 206 on a staged file;
  - an expired link returns 403 even while the stage is still cached.
- Stage cache:
  - single-flight;
  - concurrent mints for the same clip produce one DVR download;
  - eviction at expiry;
  - the `max_stage_mb` budget is respected.
- `since` / `wait_s` polling with fake clocks.
- Existing 275 tests still pass.
- On-device (Jimmy, last, briefly):
  - add-on hostname;
  - DVR/Reolink indexing delay after motion;
  - one Driveway clip playing on the frame from a minted URL.

## 9. Questions for the user
1. Link port: 8100 on the HA host, unmapped until you set it — OK?
2. Mint secret as an add-on option (`password` type), mirrored in HA `secrets.yaml` — OK?
3. Snapshots: HA camera entities already have an `entity_picture` URL with a
   rotating token. If the frame can reach HA's HTTP port, snapshots need no add-on
   code (scope cut). Otherwise we add `kind: snapshot`.
4. Motion → clip: wait server-side for the new clip (`since` + `wait_s`, recommended),
   or just return the latest closed clip?

## 10. Later (not this issue)
HA media source (clips in HA's media browser / any media player).

## 11. Decisions (Fable advisor, 2026-11-16) — override earlier sections

Context from the frame project:
- The frame is now `media_player.living_room_frame` (HA dlna_dmr). It uses an ExoPlayer
  bridge and plays HTTP MP4/fMP4/TS, HLS and RTSP; `play_media` with a URL works.
- The frame shares an L2 segment with an HA host interface.
- The frame already fetches from HA's 9123 with valid TLS (`camera.play_stream` works).

1. **Port: separate link app on 8100, `ports: {"8100/tcp": null}` (unmapped by
   default).** Not 8099: publishing 8099 would put the whole ingress app on the
   LAN behind only a peer-IP check. The only way to serve via HA's 9123 is a media
   source integration (later) or writing files to `/media`, which breaks zero
   persistence. Verify the `null` = disabled semantics against the Supervisor docs.
2. **Mint secret: a `password` option mirrored in `secrets.yaml`, minimum 24
   characters.** `hassio.addon_stdin` (Supervisor-authenticated) was rejected: it is
   one-way (no URL or error back to the automation) and would couple the add-on to
   one player.
3. **Snapshots: out of scope.** Use the camera's `entity_picture`
   (`/api/camera_proxy/...?token=`), resolved at call time and prefixed with HA's
   external URL. The README shows how. Delete `kind: snapshot` from §2/§5.
4. **Motion → clip: server-side wait (`since` + `wait_s`).** Live view is already
   `camera.play_stream`; this feature is "replay what just happened". Risk to
   measure on-device: if the DVR records continuously, no clip may close within
   90 s; a time-window clip can come later (`playbackURI` carries start/end).

Changes these make to the draft:
- **HA flow:** `rest_command` mint → `media_player.play_media` on
  `media_player.living_room_frame` with the URL and `media_content_type: video/mp4`.
  `frame_play_url` is only a fallback.
- **No firewall rule is needed** for the frame: same segment. Document that
  Supervisor publishes the port on **all** host interfaces.
- **`external_base_url` is required** when links are on (plain
  `http://<HA host>:8100`; the link port has no TLS). Mint always returns an
  absolute URL.
- **Path ends in `.mp4`** (`/m/{token}/clip.mp4`), with `Content-Type: video/mp4`.
  DLNA renderers infer the type from the extension.
- **Token exposure:** the URL lands in the player's `media_content_id` and so in
  recorder history. That is acceptable only because of the short TTL and the
  in-memory key; the docs say so.
- **Stage budget:** a clip larger than `max_stage_mb` fails at mint with a clear
  error. Keep at most 2 live link-stage entries and evict the oldest.
- **HEVC:** the on-device step confirms that an HEVC channel's fMP4 plays on the
  frame.

## 12. Goldfish resolutions (2026-11-17) — binding for the implementer

### Blockers
- **B1 One builder, two apps.** Replace the import-time `app = _build_default_app()`
  with `create_apps(config, ...) -> (ingress_app, link_app | None)` built from **one**
  closure, so both apps share `registry`, `dvr`, `reolink_by_key`, the stage helpers,
  and `HaClient`.
  - `SUPERVISOR_TOKEN` is popped exactly once, inside it.
  - Keep `create_app()` as a thin wrapper returning `[0]` for the existing tests.
  - New launcher `hikvision_recordings/app/serve.py` is the only production builder.
    `run.sh` `exec`s `python3 -m hikvision_recordings.app.serve` and passes
    `LOG_LEVEL` via env.
  - Test: with `SUPERVISOR_TOKEN` set, the launcher's ingress app has
    `IngressOnlyMiddleware.enabled` true.
- **B2 Own prefix and sweep.** Link stages use prefix `hiklink-`; `_stage_clip` takes
  the prefix as a parameter. `_sweep_stale_stages` keeps touching only `hikclip-`.
  - The link cache sweeps its own entries: on every mint and fetch, plus a 60 s
    periodic task.
  - Hard cap: an entry is deleted at most 900 s after it finished staging, whatever
    tokens remain.
  - A token's `exp` is never later than its entry's hard-cap deletion time.
  - On startup, delete any leftover `hiklink-*` files.

### Token and entries
- The token references a **link-entry id**: random 16 bytes, b64url. It does not
  reference the registry `clip_id`.
- The entry holds `Clip`, quality, staged path (DVR only), `created`, `hard_expiry`.
- Entries are de-duplicated by `(camera_key, clip start, main_ref, quality)`, so a
  re-mint reuses the entry.
- Token = `b64url(payload) "." b64url(HMAC)`.
  - `payload = {v:1, e: entry_id, x: exp}`.
  - Cap the token length at 512.
  - Verify the HMAC on the raw segment **before** JSON-decoding.
  - Any malformed token → 403.
- Served bytes come only from the entry (path, or `Clip` for Reolink). Nothing from
  the URL beyond the token touches the filesystem.
- Evicted or unknown entry with a valid signature → 410. Never re-stage on fetch.

### Mint endpoint
- The bearer check is **inside the endpoint**, for every peer, ingress included,
  and regardless of `SUPERVISOR_TOKEN`.
  - Compare as bytes: `hmac.compare_digest(a.encode(), b.encode())`.
  - Never log the `Authorization` header or the request body.
- Middleware exemption: exactly `method == "POST" and scope["path"] == "/internal/mint"`.
  `/internal/mint/` and GET stay 403 for non-ingress peers.
- `external_links` false or link config invalid:
  - the route returns 404 (off) or 503 (invalid);
  - an invalid config logs an error and does not start the link listener;
  - `load_config` never raises for link options, so the UI must keep working.
- Request: `{camera, since?, wait_s?, event?, quality?}`.
  - `camera` is a key (`dvr-101`, `reolink-0`), or an exact display name as fallback.
  - `quality` defaults to `sd`, with the existing main fallback.
  - `wait_s` is 0–60.
  - `since` is an ISO time; no timezone means UTC, as `_parse_utc` does. Future, or
    older than 24 h → 400.
  - `event` that the camera kind can't produce → 400. DVR: motion/scheduled/alarm.
    Reolink: what its filename parser yields.
- Response is always an absolute URL: `{url, expires_at, camera, start, end,
  duration_s, events, content_type: "video/mp4"}`.
- Errors (JSON `detail`):

  | Status | `detail` | Meaning |
  |---|---|---|
  | 400 | `bad_request` / `unknown_camera` / `unsupported_event` | Bad input |
  | 401 | — | Bad or missing bearer |
  | 404 | `not_ready` | No matching clip yet |
  | 413 | `too_large` | Clip over the `max_stage_mb` cap |
  | 503 | `busy` | `DvrBusy` / staging failure; retry |
  | 503 | `disabled_misconfigured` | Link config invalid |

### Clip matching (replaces §4 rule)
- Search window `[since - 30 s, now]`.
- Candidate: newest clip with `end >= since` and `start <= since + wait_s`, matching
  `event` if given.
- It must be **closed**:
  - `end <= now - 3 s`;
  - its `end` is unchanged across two consecutive polls (polls every 5 s).
- Without `since`: newest closed clip in the last 24 h, matching `event`.
- While polling the DVR, search the main track only; skip the sub search.
- Total mint bound: `wait_s` (≤ 60) plus staging. The README uses `rest_command`
  `timeout: 120` (cap `wait_s` at 60 in the example).

### Staging (DVR)
- Staging runs as a **detached task** with the existing done-callback single-flight
  pattern (`ha.py` / `thumbnail.py`). Waiters use `asyncio.shield`, so a cancelled
  mint never kills a shared stage.
- Failures are not cached.
- Mint waits for staging to finish; `exp` is computed after staging.
- `max_stage_mb` stays the **per-clip** cap; over it → 413.
- Aggregate = at most 2 link entries; evict the oldest. Worst-case RAM is
  `2 × max_stage_mb` plus the UI stages; document it.

### Reolink
- No staging. Fetch proxies Range from the entry's stored `Clip`; no registry lookup.
- HEAD answers from `clip.size_bytes` without contacting the camera.
- Link fetches may wait up to 30 s for a download slot (UI keeps 10 s).

### Link app
- Bare Starlette with exactly one `Route("/m/{token}/clip.mp4", methods=["GET"])`;
  Starlette adds HEAD.
  - No docs, openapi, static or `/api` routes.
  - Test that `/`, `/docs`, `/openapi.json`, `/api/health` and `/m/x` all return 404.
- `Content-Type: video/mp4`, `Cache-Control: no-store`, Range → 206.

### Servers and logging
- Two `uvicorn.Server`s in one event loop: 8099 starts first.
- Both use `access_log=False` and `server_header=False`.
- The link server runs at `max(user level, info)`, so never trace or debug: uvicorn
  trace dumps paths.
  - Test at TRACE with caplog that a token never appears in the logs.
- The link server uses `lifespan="off"`. Its `serve()` is wrapped so a bind or
  runtime failure logs an error and leaves 8099 running.
- On shutdown, stop the link server before the ingress lifespan closes the clients.

### Config
- `external_links: false`.
- `external_mint_token: password?`: required ≥ 24 characters when on.
- `external_link_ttl_s: int(60,900)`: default 300.
- `external_base_url: str?`: required when on.
  - http/https only; no path, query or fragment; trailing `/` stripped.
- `ports: {"8100/tcp": null}` plus `ports_description`.
- Docs note: even when unmapped, 8100 is reachable from other containers on the
  hassio network. That is acceptable because it is token-gated.

### Docs
- Firewall: issue #5 says the frame currently reaches only specific HA ports, so the
  README says "allow the player → HA host:<port> if your network filters it".
  - The homelab rule is Jimmy's.
  - Supervisor publishes the port on all host interfaces.
- Token exposure in the recorder, the TTL behaviour (a link expiring mid-playback
  fails later Range requests), worst-case RAM, and `entity_picture` for snapshots.
- HA example: `rest_command` mint (bearer `!secret`, `timeout: 120`,
  `response_variable`) → `media_player.play_media` with `media_content_type: video/mp4`.

### On-device (Jimmy, last)
- Add-on hostname.
- Whether the DVR lists open segments, and its indexing delay.
- Continuous vs event recording.
- An HEVC channel playing on the frame (no `hvc1` tag today).
- One clip end-to-end via `play_media`.

## 13. On-device results (2026-11-17) and v0.7.1
- All §12 on-device checks passed: the DVR and Reolink clips played on the frame, and
  bad or expired links were refused. No firewall change was needed. All DVR channels
  are H.264, so the HEVC check was not applicable.
- DVR motion clips close 30–70 s after the trigger, and the DVR lists them about 18 s
  after they close. It never serves an open segment.
  - The 60 s `wait_s` cap therefore made the first mint return `not_ready`.
  - v0.7.1 raises the cap to 120 s; the README example uses `timeout: 180`.
