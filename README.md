# Hikvision Recordings — Home Assistant Add-on

Browse, play and download recordings **already stored on your Hikvision DVR/NVR**.
This add-on records nothing and stores no video — it reads the DVR's own recording
index over ISAPI and streams existing clips straight through to your browser.

## Install

1. Settings → Add-ons → Add-on Store → ⋮ → Repositories
2. Add `https://github.com/jimmyrelismartinez/ha-hikvision-recordings`
3. Install **DVR Recordings**, open Configuration, fill in:
   - `dvr_host` — DVR IP (e.g. `10.10.11.56`)
   - `dvr_username` / `dvr_password` — a DVR account with playback rights
   - `channels` — one row per camera: `id` (e.g. `101`) and `name` (e.g. `Driveway`)
4. Start. The panel appears in the sidebar as **Recordings**.

> Add-on options are stored by Supervisor in `/data/options.json` in plaintext — standard
> for every HA add-on. Use a limited playback/operator DVR account, not `admin`.

## Notes

- Seeking inside a clip is not supported (the DVR streams one-shot, no HTTP Range).
  Playback works; to scrub, download the clip.
- Base image: `3.12-alpine3.22` — common to `aarch64-base-python`, `armv7-base-python`, and
  `amd64-base-python` (verified: manifest pulls HTTP 200 on all three, 2026-07-25). Matches
  the dev `.venv` Python (3.12.3) exactly — no dev/prod runtime skew. Task 10's `build.yaml`
  may use this single tag string for all three arches.
- Install target: HAOS core at https://10.10.10.9:9123; jimmy-ha on :8124 is a HA Container
  with no add-on store (verified by probe 2026-07-24: `/api/hassio/*` → 404 on :8124; HA
  frontend + 401 on `/api/` at :9123).
- Slug collision check + arch confirmation on the :9123 instance is PENDING — owner to verify
  in the HA UI before install (requires human login; not checked programmatically).
