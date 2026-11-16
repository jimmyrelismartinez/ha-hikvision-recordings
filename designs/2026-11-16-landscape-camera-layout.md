# Landscape camera page: video on top, horizontal filmstrip + day bar at the bottom (v0.6.0)

Status: draft → goldfish review → implement
Scope: frontend only (`www/`), camera page (`#/camera/<key>`) only.

## 1. Problem

On a PC (landscape, e.g. 1280×800 / 1920×1080) the camera page stacks: header →
toolbar → pinned player (video `max-height: 60vh` + quality bar + nav) → sticky
filter bar → vertical timeline. The pinned player eats almost the whole viewport,
so the vertical list under it shows about one clip at a time.

User decision (2026-11-16):
- Landscape desktop/tablet: video on top fills the free space; at the bottom a
  **day bar** (24 h, clip segments) above a **horizontal filmstrip** of clip
  thumbnails, scrolling left/right.
- Portrait and phones (any orientation): keep today's layout unchanged.
- Only the camera page. Home and "All recordings" unchanged.

## 2. Activation

```js
const WIDE_MQ = matchMedia('(orientation: landscape) and (min-width: 1000px) and (min-height: 600px)');
```

- Phones in landscape are ~390–430 px tall → excluded by `min-height: 600px`.
- A PC browser window narrower than 1000 px, or taller than wide, keeps the
  current layout. iPad landscape (≥1024×768) gets the wide layout.
- `isWide()` = `WIDE_MQ.matches`. Body class `layout-wide` is set only when
  `isWide() && route.name === 'camera'` (set in `applyRoute` and on MQ `change`).
- CSS for the wide layout is keyed **only** on `body.view-camera.layout-wide`, so
  no other page can be affected, and the JS flag and CSS can never disagree.

## 3. Layout (CSS)

`body.view-camera.layout-wide`:
- `height: 100dvh` (fallback `100vh`), `overflow: hidden`, existing flex column
  (`#view-camera` is already `display: contents`).
- header, `.camera-toolbar`: `flex: none` (compact margins).
- `#player-panel`: `flex: 1 1 auto; min-height: 0; position: static;` laid out as
  a flex column; `.player-body` / `.stage-live` take `flex: 1; min-height: 0`; the
  `<video>` and `.stage-image` get `width: 100%; height: 100%; max-height: none;
  object-fit: contain`. Quality bar + nav stay `flex: none`. `keys-hint` hidden.
- `.camera-chrome` (bottom dock): `flex: none; height: clamp(210px, 30vh, 300px)`,
  flex column: filter bar (`.timeline-sticky`, `position: static`) → day bar →
  filmstrip (`flex: 1; min-height: 0`).
- `--player-sticky-offset` is 0 in wide mode (`syncPlayerStickyOffset` already
  only pins when computed `position === 'sticky'`).
- The page itself never scrolls in wide mode; only the filmstrip scrolls (x).

## 4. Filmstrip (replaces the vertical timeline in wide mode)

Rendering: `renderVTimeline()` becomes a dispatcher: wide → `renderFilmstrip()`,
else the existing vertical renderer (unchanged). Both render into `#vtimeline`
(class toggled `vtimeline` / `filmstrip`) so `markActive`, `openClip` scroll,
`?t=` reconcile and thumbnail queue keep one container.

- Order: **chronological ascending, oldest left, newest right** (reads like time).
- Per day: a day separator element (`.fs-day`, label `dayLabel(day)`, `data-day`)
  before that day's cards; "Now" marker at the right end of today.
- Card (`.fs-card`, a `<button>`, `data-clip-key`, `data-clip-id`): 16:9 thumb
  (~168 px wide, via `attachThumb`), time (`shortTime`), duration, event badges.
  Card height fits the dock; `.active` = accent border.
- Gaps ≥ `GAP_BAND_MIN_S` between consecutive visible clips: narrow `.fs-gap`
  label ("2 h 10 m") between cards.
- Filter: non-passing cards are not rendered (no dimming in the strip); the day bar
  still shows them dimmed. A day with clips but none passing → small
  "No <filter> clips" placeholder in that day group.
- Day error → `.vday-error`-style inline retry in that day group (same handler).
- Empty day → small "No recordings" placeholder card for that day (keeps the day
  visible so the date chip / day bar can land on it).
- Truncated day → "(partial)" in the day separator, title tooltip.
- Mouse wheel: when the wheel event has |deltaY| > |deltaX| over the filmstrip,
  convert to horizontal scroll (`scrollLeft += deltaY`, `preventDefault`,
  `{ passive: false }`). Trackpad horizontal swipes work natively.
- Thumbnails: the existing `thumbObserver` uses the viewport as root; cards in the
  horizontal scroller outside its box are clipped, not intersecting, so the 2-in-
  flight queue still only loads what's visible. Verify; if not, use a second IO
  with `root: filmstrip`.

### Initial scroll position
- No `?t` and nothing playing: scroll to the **right end** (newest).
- `?t` / playing clip: `scrollIntoView({ inline: 'center', block: 'nearest' })` on
  its card. **`block: 'nearest'` everywhere in wide mode** so the page never
  scrolls vertically.
- `openClip` (Earlier/Later/autoplay): same, `behavior: 'smooth'`.

### Loading older days (left edge)
- `#day-sentinel` is placed as the **first child** of the filmstrip (left end) in
  wide mode; in vertical mode it stays after the list (move the node on mode
  switch; it is not the player, so re-parenting is fine).
- The sentinel IO uses `root: filmstrip` in wide mode (`rootMargin: '0px 300px'`),
  `root: null` (current) otherwise. `sentinelStillVisible()` checks against the
  filmstrip's rect in wide mode.
- **Scroll preservation on prepend**: older days insert to the left, which would
  shift visible cards right. Before re-render record
  `fromRight = scrollWidth - scrollLeft`; after, set
  `scrollLeft = scrollWidth - fromRight`. (Horizontal scroll anchoring is not
  reliable cross-browser; do it explicitly.) Applies to every wide re-render
  that isn't an explicit jump (filter change re-renders keep the active/centered
  card in view instead: re-center on the card that was nearest the center).
- "Load earlier day" / "No older recordings" / pausedAuto UI: shown inside the
  strip at the left end (before the sentinel) as a compact button/label, same
  state machine (`updateStopUi`), only placement differs.

## 5. Day bar

`.daybar` above the filmstrip, for the **focused day**:
- Focused day = day of the card nearest the filmstrip's horizontal center
  (updated on scroll, rAF-throttled). This replaces `updateDateChipFromScroll` in
  wide mode; the date chip shows the same day.
- 0–24 h track, ticks at 00 06 12 18 24 (`localTime` labels), a segment per clip
  (`left = (start - dayStart)/24h`, `width ≥ 3px`), non-passing clips dimmed,
  active clip highlighted, "now" line on today.
- Segment click → `openClip(clip)` (and centers its card). Segments are buttons
  with `aria-label` "<time>, <duration>". No drag/scrub, no seek inside a clip.
- DST days: compute from local `startOfLocalDay(d)` and `addLocalDays(d, 1)` so
  the track spans the real day length (23/25 h).

## 6. Mode switches

On `WIDE_MQ` `change` while on the camera page:
- toggle `layout-wide`, move sentinel, re-attach sentinel IO with the right root,
  re-render via `renderVTimeline()`, then scroll the active card (or newest if
  none) into view. The `<video>` element is untouched (never re-parented), so
  playback continues.
- `applyRoute` leaving the camera page removes `layout-wide`.

## 7. Unchanged

Phones/portrait camera page, home, All recordings, backend, snapshot cadence,
thumbnail queue limits, 410 recovery, keyboard shortcuts (j/k/←/→/space/f), date
jump (`jumpCameraToDay` → in wide mode scroll the strip to that day's separator).

## 8. Testing

Browser harness (`tests/frontend/test_fallback.html`, run by `tests/frontend/run.py`):
- MQ is stubbed (inject `window.__forceWide = true|false` or stub `matchMedia`).
- wide: cards ascending; day separators in order; filter hides cards but day bar
  keeps dimmed segments; segment click opens the right clip; active card marked;
  prepend of an older day keeps `scrollWidth - scrollLeft` constant; sentinel is
  the filmstrip's first child; wheel → horizontal; mode switch re-renders without
  disposing `current` and keeps the same `<video>` node.
- narrow: existing vertical tests unchanged (regression).
- Mutation-check at least the prepend-scroll and mode-switch tests.

Screenshots (mock server): 1280×800, 1920×1080, 1024×768 (wide); 390×844 and
844×390 (unchanged phone layouts).

## 9. Release

Version 0.6.0 (`config.yaml`, test_api version assertion, README section
"Landscape layout").
