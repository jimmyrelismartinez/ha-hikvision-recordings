# External playback: short-lived signed clip/snapshot links (issue #5) — DRAFT

Status: draft plan, awaiting user answers (§9) → goldfish review → implement
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
