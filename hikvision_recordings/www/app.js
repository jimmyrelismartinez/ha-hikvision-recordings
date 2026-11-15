/* ── INGRESS RULE — EVERY URL IN THIS FILE IS RELATIVE ────────────────────────
 * Home Assistant serves this add-on under a rotating prefix:
 *     /api/hassio_ingress/<token>/
 * A leading slash — fetch("/api/channels") — escapes that prefix, hits HA core
 * and 404s. This is the single most common way HA add-on frontends break.
 * Use "api/channels", never "/api/channels". Same for <script>, <link>, <video>.
 * ──────────────────────────────────────────────────────────────────────────── */

const $ = (id) => document.getElementById(id);
const chipsEl = $('camera-chips');
const dateInput = $('date');
const fromInput = $('from');
const toInput = $('to');
const form = $('search-form');
const results = $('results');
const message = $('message');
const statusEl = $('status');
const searchButton = $('search-button');
const bannersEl = $('banners');
const timelineEl = $('timeline');
const showMoreButton = $('show-more');
const playerPanel = $('player-panel');
const playerBody = $('player-body');
const playerCamera = $('player-camera');
const playerTime = $('player-time');
const prevButton = $('prev-clip');
const nextButton = $('next-clip');
const autoplayInput = $('autoplay');
const closeButton = $('player-close');

const pad = (n) => String(n).padStart(2, '0');
const localDate = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const localTime = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;
const clockTime = (d) => d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

// Rows drawn up front; the rest arrive with "Show more". The timeline always
// draws every segment — it is cheap, and it is the overview.
const PAGE_ROWS = 200;

/* ── Persisted preferences ───────────────────────────────────────────────────
 * localStorage can throw (private mode, a locked-down WebView) — preferences are
 * a nicety, so any failure just means "use the default".
 * ─────────────────────────────────────────────────────────────────────────── */
const STORE = { cameras: 'dvr-rec.cameras', autoplay: 'dvr-rec.autoplay', quality: 'dvr-rec.quality' };
function loadPref(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    return raw === null ? fallback : JSON.parse(raw);
  } catch (err) { return fallback; }
}
function savePref(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch (err) { /* ignore */ }
}

function showMessage(text, kind = 'info') {
  message.textContent = text;
  message.className = `message ${kind}`;
  message.hidden = !text;
}

function formatSize(bytes) {
  if (!bytes) return '';
  const mb = bytes / (1024 * 1024);
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb.toFixed(0)} MB`;
}

function formatDuration(seconds) {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m ? `${m}m ${pad(s)}s` : `${s}s`;
}

/* ── Cameras: "All" plus one chip per camera, multi-select ───────────────────
 * An empty selection means "all". Picking every camera one by one collapses
 * back to "All", so a camera added later is included automatically.
 * ─────────────────────────────────────────────────────────────────────────── */
let cameras = [];                 // [{key, name, kind}] from api/channels
let selectedCameras = new Set();  // empty = all

function cameraName(key) {
  const cam = cameras.find((c) => c.key === key);
  return cam ? cam.name : key;
}

function renderChips() {
  chipsEl.innerHTML = '';
  const make = (label, key, pressed) => {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'chip';
    chip.textContent = label;
    chip.dataset.camera = key;
    chip.setAttribute('aria-pressed', String(pressed));
    chip.addEventListener('click', () => toggleCamera(key));
    chipsEl.appendChild(chip);
  };
  make('All', 'all', selectedCameras.size === 0);
  for (const cam of cameras) make(cam.name, cam.key, selectedCameras.has(cam.key));
}

function toggleCamera(key) {
  if (key === 'all') {
    selectedCameras.clear();
  } else if (selectedCameras.has(key)) {
    selectedCameras.delete(key);
  } else {
    selectedCameras.add(key);
  }
  if (selectedCameras.size === cameras.length) selectedCameras.clear();
  savePref(STORE.cameras, [...selectedCameras]);
  renderChips();
}

function selectedKeys() {
  return selectedCameras.size ? [...selectedCameras] : cameras.map((c) => c.key);
}

async function loadChannels() {
  const response = await fetch('api/channels');
  if (!response.ok) throw new Error('Could not load the camera list.');
  const channels = await response.json();
  cameras = channels.map((c) => ({ key: String(c.key ?? c.id), name: c.name, kind: c.kind || 'dvr' }));
  const saved = loadPref(STORE.cameras, []);
  const known = new Set(cameras.map((c) => c.key));
  selectedCameras = new Set(Array.isArray(saved) ? saved.filter((k) => known.has(k)) : []);
  if (selectedCameras.size === cameras.length) selectedCameras.clear();
  renderChips();
}

async function loadHealth() {
  try {
    const health = await (await fetch('api/health')).json();
    const devices = Array.isArray(health.devices) && health.devices.length
      ? health.devices
      : [{ name: 'DVR', state: health.dvr }];
    const down = devices.filter((d) => d.state !== 'ok');
    if (!down.length) {
      statusEl.textContent = devices.length === 1 ? `${devices[0].name} online` : 'All devices online';
      statusEl.className = 'status ok';
    } else {
      statusEl.textContent = down.map((d) => (d.state === 'auth_failed'
        ? `${d.name} rejected the credentials`
        : `${d.name} unreachable`)).join(' · ');
      statusEl.className = 'status bad';
    }
    if (typeof health.client_remux_max_mb === 'number' && health.client_remux_max_mb > 0) {
      CLIENT_REMUX_MAX_BYTES = health.client_remux_max_mb * 1024 * 1024;
    }
  } catch (err) {
    statusEl.textContent = '';
  }
}

function applyPreset(preset) {
  const now = new Date();
  if (preset === '1h' || preset === '6h') {
    const hours = preset === '1h' ? 1 : 6;
    const start = new Date(now.getTime() - hours * 3600 * 1000);
    dateInput.value = localDate(start);
    fromInput.value = localTime(start);
    toInput.value = localTime(now);
  } else if (preset === 'today') {
    dateInput.value = localDate(now);
    fromInput.value = '00:00';
    toInput.value = '23:59';
  } else if (preset === 'yesterday') {
    const yesterday = new Date(now.getTime() - 86400 * 1000);
    dateInput.value = localDate(yesterday);
    fromInput.value = '00:00';
    toInput.value = '23:59';
  }
}

/* ── Lazy thumbnails ─────────────────────────────────────────────────────────
 * Thumbnails share the DVR's max_concurrent_downloads budget with playback and
 * downloads, so firing 40 requests the moment a search returns would swamp the
 * device and make the list slow. Each row's image is fetched only once it
 * actually scrolls into view, and only once (unobserved immediately after).
 * A failed thumbnail leaves that one row on its placeholder — it must never
 * break the rest of the list.
 * ─────────────────────────────────────────────────────────────────────────── */
const thumbObserver = ('IntersectionObserver' in window)
  ? new IntersectionObserver((entries, obs) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const img = entry.target;
        obs.unobserve(img);
        img.src = img.dataset.src;   // relative URL — see the ingress rule above
      }
    }, { rootMargin: '200px' })      // start a little before the row is visible
  : null;

function attachThumb(item, clip) {
  const thumb = document.createElement('div');
  thumb.className = 'clip-thumb';
  const img = document.createElement('img');
  img.alt = '';
  img.loading = 'lazy';
  img.dataset.src = `api/thumbnail/${clip.id}`;   // relative — no leading slash
  img.addEventListener('load', () => thumb.classList.add('loaded'));
  img.addEventListener('error', () => thumb.classList.add('failed'));
  thumb.appendChild(img);
  item.prepend(thumb);
  if (thumbObserver) {
    thumbObserver.observe(img);
  } else {
    img.src = img.dataset.src;       // no IntersectionObserver: just load it
  }
}

/* ── PLAYBACK: client-side remux, with the server path as the safety net ──────
 * FAST PATH  — fetch api/stream-raw (the DVR's untouched MPEG-PS), remux it to MP4
 *              in this browser with ffmpeg.wasm, play it from a blob URL. No ffmpeg
 *              runs on the Pi, nothing is staged, and because the whole MP4 is in
 *              memory the result is genuinely SEEKABLE — which the server path
 *              cannot offer.
 * SAFE PATH  — api/stream, i.e. the tmpfs-staged server-side remux from v0.1.1/
 *              v0.1.3, untouched. Used automatically whenever the fast path is
 *              unavailable or fails for ANY reason.
 *
 * NO SharedArrayBuffer, NO COOP/COEP. That requirement belonged to ffmpeg.wasm's
 * multi-threaded core. Cross-origin isolation is inherited from the top-level
 * document, and HA's frontend sends neither header, so inside the Ingress iframe
 * crossOriginIsolated is always false and nothing this add-on serves can change
 * that. The single-threaded core needs none of it — see the Dockerfile note.
 * ─────────────────────────────────────────────────────────────────────────── */

// Above this, the fast path is skipped: the raw clip plus its remuxed copy both
// live in the wasm heap, and on a phone (HA Companion's WKWebView is the real
// target) a large clip will exhaust memory rather than throw something catchable.
// Compared against the MAINSTREAM size from the search result, which is the
// conservative direction — an SD fetch is roughly half of it.
let CLIENT_REMUX_MAX_BYTES = 128 * 1024 * 1024;

const QUALITIES = ['sd', 'hd'];
const DEFAULT_QUALITY = 'sd';

let ffmpegPromise = null;

function ffmpegAvailable() {
  return typeof FFmpegWASM !== 'undefined' && typeof WebAssembly !== 'undefined';
}

async function getFFmpeg() {
  if (!ffmpegPromise) {
    ffmpegPromise = (async () => {
      const { FFmpeg } = FFmpegWASM;
      const ff = new FFmpeg();
      // NOTE: classWorkerURL is deliberately NOT passed. In the UMD build that
      // option forces `new Worker(..., {type:"module"})`, where importScripts()
      // does not exist, so loading the UMD core falls into a dead code path and
      // throws "Cannot find module". Omitting it yields a classic worker, and the
      // worker file is then resolved relative to vendor/ffmpeg.js — which is why
      // 814.ffmpeg.js must sit beside it.
      await ff.load({
        coreURL: new URL('vendor/ffmpeg-core.js', document.baseURI).href,
        wasmURL: new URL('vendor/ffmpeg-core.wasm', document.baseURI).href,
      });
      return ff;
    })().catch((err) => {
      ffmpegPromise = null;      // let a later clip retry rather than poisoning them all
      throw err;
    });
  }
  return ffmpegPromise;
}

// One wasm instance with one filesystem: two clips remuxing at once would collide.
// Serialise them instead of instantiating a second 32 MB core.
let remuxChain = Promise.resolve();
function serialise(task) {
  const run = remuxChain.then(task, task);
  remuxChain = run.catch(() => {});
  return run;
}

async function remuxInBrowser(clip, quality) {
  const ff = await getFFmpeg();
  const response = await fetch(`api/stream-raw/${clip.id}?quality=${quality}`);
  if (!response.ok) {
    let detail = `the add-on returned HTTP ${response.status}`;
    try { detail = (await response.json()).detail || detail; } catch (err) { /* not JSON */ }
    throw new Error(detail);
  }
  const raw = new Uint8Array(await response.arrayBuffer());
  if (!raw.length) throw new Error('the DVR returned an empty clip');

  // Unique names: MEMFS is shared across every clip this session.
  const stamp = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const input = `in-${stamp}.ps`;
  const output = `out-${stamp}.mp4`;
  try {
    await ff.writeFile(input, raw);
    // The same stream copy the server does — no re-encode. Plain MP4 rather than
    // the server's fragmented one: the whole file is already here, so a normal
    // moov gives the browser full seeking.
    const code = await ff.exec(['-hide_banner', '-loglevel', 'error',
                                '-i', input, '-c', 'copy', '-f', 'mp4', output]);
    if (code !== 0) throw new Error(`ffmpeg.wasm exited ${code}`);
    const data = await ff.readFile(output);
    if (!data || !data.length) throw new Error('ffmpeg.wasm produced no output');
    return new Blob([data.buffer], { type: 'video/mp4' });
  } finally {
    // Always reclaim the wasm heap, even on failure — otherwise a few large clips
    // exhaust it and every later remux fails for an unrelated reason.
    for (const path of [input, output]) {
      try { await ff.deleteFile(path); } catch (err) { /* never existed */ }
    }
  }
}

/* Why the fast path was skipped, in words Ramon can read off a screenshot. */
function clientRemuxBlocker(clip) {
  if (!ffmpegAvailable()) return 'ffmpeg.wasm not loaded in this browser';
  if (clip.size_bytes > CLIENT_REMUX_MAX_BYTES) {
    return `clip is ${formatSize(clip.size_bytes)}, over the ${formatSize(CLIENT_REMUX_MAX_BYTES)} in-browser limit`;
  }
  return null;
}

function setMode(badge, mode, detail) {
  badge.dataset.mode = mode;
  badge.textContent = {
    working: 'Remuxing in browser…',
    fast: 'Fast · browser remux',
    compat: 'Compatibility · server remux',
    direct: 'Direct · native MP4',
    failed: 'Playback failed',
  }[mode] || mode;
  badge.title = detail || '';
}

/* ── One player ──────────────────────────────────────────────────────────────
 * buildPlayer() renders the SD/HD picker, the which-path-ran badge, the video and
 * the Download link for ONE clip into `body`, and starts playback. The page panel
 * passes its own long-lived <video> in `opts.video`: iOS grants autoplay to an
 * element the user has already started, so reusing it is what lets "autoplay
 * next" actually play without another tap.
 *
 *   opts.quality    initial 'sd' | 'hd' (default sd)
 *   opts.onQuality  called with the new quality when the user switches
 *   opts.onEnded    called when the clip plays to the end
 *   opts.video      a <video> to reuse
 *
 * Returns { video, dispose }. dispose() stops playback and frees any blob, and
 * makes a remux still in flight discard its result instead of grabbing the video.
 * ─────────────────────────────────────────────────────────────────────────── */
function buildPlayer(clip, body, opts = {}) {
  let quality = QUALITIES.includes(opts.quality) ? opts.quality : DEFAULT_QUALITY;
  let blobUrl = null;
  let generation = 0;     // bumps on every play() and on dispose()

  const video = opts.video || document.createElement('video');
  video.controls = true;
  video.playsInline = true;
  video.setAttribute('playsinline', '');
  video.preload = 'none';
  video.onended = () => { if (opts.onEnded) opts.onEnded(); };

  const bar = document.createElement('div');
  bar.className = 'clip-controls';

  const picker = document.createElement('div');
  picker.className = 'quality';
  picker.setAttribute('role', 'group');
  picker.setAttribute('aria-label', 'Quality');
  const buttons = new Map();
  for (const q of QUALITIES) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = q.toUpperCase();
    button.title = q === 'sd'
      ? 'Substream — smaller and faster to load'
      : 'Mainstream — full resolution';
    button.addEventListener('click', () => {
      if (quality === q) return;
      quality = q;
      for (const [key, el] of buttons) {
        el.classList.toggle('on', key === quality);
        el.setAttribute('aria-pressed', String(key === quality));
      }
      updateDownload();
      if (opts.onQuality) opts.onQuality(quality);
      play();
    });
    buttons.set(q, button);
    picker.appendChild(button);
  }
  for (const [key, el] of buttons) {
    el.classList.toggle('on', key === quality);
    el.setAttribute('aria-pressed', String(key === quality));
  }

  const badge = document.createElement('span');
  badge.className = 'mode-badge';

  bar.append(picker, badge);

  const hint = document.createElement('p');
  hint.className = 'hint';

  const download = document.createElement('a');
  download.className = 'download';
  download.setAttribute('download', '');
  function updateDownload() {
    // Download follows the SD/HD toggle (v0.2.0); the server defaults to HD.
    download.href = `api/download/${clip.id}?quality=${quality}`;   // relative — see rule at top
    download.textContent = `Download ${quality.toUpperCase()}`;
  }
  updateDownload();

  body.append(bar, video, hint, download);

  function freeBlob() {
    if (blobUrl) { URL.revokeObjectURL(blobUrl); blobUrl = null; }
  }

  function useServerPath(reason) {
    setMode(badge, 'compat', reason);
    hint.textContent = "Seeking isn't supported on this path — download the clip to scrub.";
    video.src = `api/stream/${clip.id}?quality=${quality}`;   // relative — see rule at top
    video.play().catch(() => { /* iOS wants a second tap; the controls handle it */ });
  }

  async function play() {
    const mine = ++generation;
    freeBlob();

    if (clip.playback === 'direct') {
      // Reolink: the camera already serves browser-playable MP4 and the add-on
      // proxies it with Range — no ffmpeg anywhere, and seeking works.
      setMode(badge, 'direct', 'camera MP4 proxied as-is — seeking works');
      hint.textContent = '';
      video.src = `api/stream/${clip.id}?quality=${quality}`;   // relative — see rule at top
      video.play().catch(() => { /* iOS wants a second tap */ });
      return;
    }

    const blocker = clientRemuxBlocker(clip);
    if (blocker) {
      // Never going to work here — don't load a 32 MB core to find that out, and
      // don't show an error for something that was never attempted.
      useServerPath(blocker);
      return;
    }

    setMode(badge, 'working', 'fetching raw clip and remuxing with ffmpeg.wasm');
    try {
      const blob = await serialise(() => remuxInBrowser(clip, quality));
      if (mine !== generation) return;    // switched quality, or the player closed
      blobUrl = URL.createObjectURL(blob);
      video.src = blobUrl;
      setMode(badge, 'fast', 'remuxed in-browser by ffmpeg.wasm — seeking works');
      hint.textContent = '';
      video.play().catch(() => { /* iOS wants a second tap */ });
    } catch (err) {
      if (mine !== generation) return;
      // ANY client-side failure falls through to the path that already works.
      console.warn('client remux failed; using the server path', err);
      useServerPath(`client remux failed: ${err && err.message ? err.message : err}`);
    }
  }

  function dispose() {
    generation++;
    video.onended = null;
    try { video.pause(); } catch (err) { /* not started */ }
    video.removeAttribute('src');
    try { video.load(); } catch (err) { /* aborts the old request */ }
    freeBlob();
  }

  play();
  return { video, dispose };
}

/* ── Page state ──────────────────────────────────────────────────────────────
 * The API returns clips newest-first, which is how the list reads. Prev/next and
 * autoplay go FORWARD IN TIME (owner decision): "next" is the clip that starts
 * later, across every camera in the result — so they walk `clipsAsc`.
 * ─────────────────────────────────────────────────────────────────────────── */
let clipsNewest = [];
let clipsAsc = [];
let ascIndexById = new Map();
let rowsShown = 0;
let current = null;               // { clip, player }
const savedQuality = loadPref(STORE.quality, DEFAULT_QUALITY);
let preferredQuality = QUALITIES.includes(savedQuality) ? savedQuality : DEFAULT_QUALITY;
const panelVideo = document.createElement('video');

function setClips(clips) {
  clipsNewest = clips.slice();
  clipsAsc = clips.slice().sort((a, b) => (Date.parse(a.start) - Date.parse(b.start))
    || String(a.camera_key).localeCompare(String(b.camera_key))
    || String(a.id).localeCompare(String(b.id)));
  ascIndexById = new Map(clipsAsc.map((c, i) => [c.id, i]));
}

function neighbourIndex(direction) {
  if (!current) return -1;
  const i = ascIndexById.get(current.clip.id);
  if (i !== undefined) {
    const j = i + direction;
    return j >= 0 && j < clipsAsc.length ? j : -1;
  }
  // The playing clip is not in the latest search: step by time instead.
  const t = Date.parse(current.clip.start);
  if (direction > 0) return clipsAsc.findIndex((c) => Date.parse(c.start) > t);
  for (let j = clipsAsc.length - 1; j >= 0; j--) if (Date.parse(clipsAsc[j].start) < t) return j;
  return -1;
}

function updateNav() {
  prevButton.disabled = neighbourIndex(-1) < 0;
  nextButton.disabled = neighbourIndex(1) < 0;
}

function markActive(id) {
  for (const el of document.querySelectorAll('[data-clip-id]')) {
    el.classList.toggle('active', el.dataset.clipId === String(id));
  }
}

function openClip(index) {
  const clip = clipsAsc[index];
  if (!clip) return;
  if (current) current.player.dispose();
  playerBody.innerHTML = '';
  playerPanel.hidden = false;
  const start = new Date(clip.start);
  playerCamera.textContent = clip.camera_name || clip.channel_name || cameraName(clip.camera_key);
  playerTime.textContent = `${start.toLocaleDateString()} ${clockTime(start)} · ${formatDuration(clip.duration_s)}`;
  const player = buildPlayer(clip, playerBody, {
    video: panelVideo,
    quality: preferredQuality,
    onQuality: (q) => { preferredQuality = q; savePref(STORE.quality, q); },
    onEnded: () => {
      if (!autoplayInput.checked) return;
      const next = neighbourIndex(1);
      if (next >= 0) openClip(next);
    },
  });
  current = { clip, player };
  updateNav();
  markActive(clip.id);
}

function step(direction) {
  const j = neighbourIndex(direction);
  if (j >= 0) openClip(j);
}

function closePlayer() {
  if (current) current.player.dispose();
  current = null;
  playerBody.innerHTML = '';
  playerPanel.hidden = true;
  markActive(null);
}

/* ── List ─────────────────────────────────────────────────────────────────── */
function renderClip(clip) {
  const item = document.createElement('li');
  item.className = 'clip';
  item.dataset.clipId = clip.id;

  const start = new Date(clip.start);
  const header = document.createElement('button');
  header.type = 'button';
  header.className = 'clip-header';

  const timeSpan = document.createElement('span');
  timeSpan.className = 'clip-time';
  timeSpan.textContent = clockTime(start);

  const metaSpan = document.createElement('span');
  metaSpan.className = 'clip-meta';
  metaSpan.textContent = `${formatDuration(clip.duration_s)} · ${formatSize(clip.size_bytes)}`;

  const chipSpan = document.createElement('span');
  chipSpan.className = 'clip-chip';
  chipSpan.textContent = clip.camera_name || clip.channel_name || cameraName(clip.camera_key);

  header.append(timeSpan, metaSpan, chipSpan);
  header.addEventListener('click', () => {
    const index = ascIndexById.get(clip.id);
    if (index !== undefined) openClip(index);
  });

  item.append(header);
  attachThumb(item, clip);
  // The thumbnail is part of the tap target too — it is the biggest thing on a card.
  item.querySelector('.clip-thumb').addEventListener('click', () => header.click());
  return item;
}

function renderMoreRows() {
  const next = clipsNewest.slice(rowsShown, rowsShown + PAGE_ROWS);
  for (const clip of next) results.appendChild(renderClip(clip));
  rowsShown += next.length;
  const left = clipsNewest.length - rowsShown;
  showMoreButton.hidden = left <= 0;
  showMoreButton.textContent = `Show more (${left} left)`;
  if (current) markActive(current.clip.id);
}

/* ── Timeline: one lane per camera over the searched window ─────────────────
 * Plain absolutely-positioned buttons — no canvas, no library. Every segment is
 * drawn (the list is paged, the overview is not). Tapping one opens that clip.
 * ─────────────────────────────────────────────────────────────────────────── */
const TICK_STEPS_MIN = [5, 10, 15, 30, 60, 120, 180, 360];

function renderTimeline(windowStart, windowEnd, laneKeys) {
  timelineEl.innerHTML = '';
  const t0 = windowStart.getTime();
  const span = windowEnd.getTime() - t0;
  if (!(span > 0)) { timelineEl.hidden = true; return; }
  const pct = (t) => `${Math.min(100, Math.max(0, ((t - t0) / span) * 100))}%`;

  // Axis: about six to eight ticks on local wall-clock boundaries.
  const stepMin = TICK_STEPS_MIN.find((m) => span / (m * 60000) <= 8) || 720;
  const axis = document.createElement('div');
  axis.className = 'tl-row tl-axis';
  axis.innerHTML = '<span class="tl-label"></span>';
  const axisTrack = document.createElement('div');
  axisTrack.className = 'tl-track';
  const tick = new Date(t0);
  tick.setSeconds(0, 0);
  const minutes = tick.getHours() * 60 + tick.getMinutes();
  tick.setHours(0, Math.ceil(minutes / stepMin) * stepMin);
  for (; tick.getTime() <= t0 + span; tick.setTime(tick.getTime() + stepMin * 60000)) {
    const mark = document.createElement('span');
    mark.className = 'tl-tick';
    const at = (tick.getTime() - t0) / span;
    mark.style.left = pct(tick.getTime());
    if (at < 0.04) mark.classList.add('edge-start');
    if (at > 0.96) mark.classList.add('edge-end');
    mark.textContent = localTime(tick);
    axisTrack.appendChild(mark);
  }
  axis.appendChild(axisTrack);
  timelineEl.appendChild(axis);

  const now = Date.now();
  for (const key of laneKeys) {
    const row = document.createElement('div');
    row.className = 'tl-row';
    row.dataset.camera = key;
    const label = document.createElement('span');
    label.className = 'tl-label';
    label.textContent = cameraName(key);
    const track = document.createElement('div');
    track.className = 'tl-track';
    clipsAsc.forEach((clip, index) => {
      if (String(clip.camera_key) !== key) return;
      const s = Math.max(Date.parse(clip.start), t0);
      const e = Math.min(Date.parse(clip.end), t0 + span);
      const seg = document.createElement('button');
      seg.type = 'button';
      seg.className = 'tl-seg';
      seg.dataset.clipId = clip.id;
      seg.style.left = pct(s);
      seg.style.width = `${Math.max(0, ((e - s) / span) * 100)}%`;
      const when = clockTime(new Date(clip.start));
      seg.title = `${cameraName(key)} ${when} · ${formatDuration(clip.duration_s)}`;
      seg.setAttribute('aria-label', `Play ${seg.title}`);
      seg.addEventListener('click', () => openClip(index));
      track.appendChild(seg);
    });
    if (now >= t0 && now <= t0 + span) {
      const marker = document.createElement('span');
      marker.className = 'tl-now';
      marker.style.left = pct(now);
      marker.title = 'Now';
      track.appendChild(marker);
    }
    row.append(label, track);
    timelineEl.appendChild(row);
  }
  timelineEl.hidden = false;
}

/* ── Search ───────────────────────────────────────────────────────────────── */
function showSkeletons() {
  results.innerHTML = '';
  for (let i = 0; i < 6; i++) {
    const li = document.createElement('li');
    li.className = 'clip skeleton';
    li.setAttribute('aria-hidden', 'true');
    li.innerHTML = '<div class="clip-thumb"></div><div class="clip-header"><span class="bar"></span></div>';
    results.appendChild(li);
  }
}

function renderBanners(errors) {
  bannersEl.innerHTML = '';
  for (const err of errors || []) {
    const banner = document.createElement('p');
    banner.className = 'banner bad';
    banner.textContent = `${cameraName(err.camera)}: ${err.detail}`;
    bannersEl.appendChild(banner);
  }
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  searchButton.disabled = true;
  showSkeletons();
  renderBanners([]);
  timelineEl.hidden = true;
  showMoreButton.hidden = true;

  const start = new Date(`${dateInput.value}T${fromInput.value}:00`);
  const end = new Date(`${dateInput.value}T${toInput.value}:59`);
  // "From" can be later in the clock than "To" (e.g. a "Last 6 h" preset run at 00:30,
  // or someone typing 23:00 → 01:00 by hand) — both mean the range crosses midnight into
  // the next day. Roll `end` forward a day rather than rejecting or silently mangling it.
  if (end <= start) {
    end.setDate(end.getDate() + 1);
  }
  showMessage(`Searching ${localDate(start)} ${localTime(start)} → ${localDate(end)} ${localTime(end)}…`);

  const laneKeys = selectedKeys();
  const params = new URLSearchParams();
  if (selectedCameras.size) {
    for (const key of selectedCameras) params.append('camera', key);
  } else {
    params.append('camera', 'all');
  }
  params.append('start', start.toISOString());
  params.append('end', end.toISOString());

  try {
    const response = await fetch(`api/recordings?${params.toString()}`);
    const body = await response.json();
    results.innerHTML = '';
    if (!response.ok) {
      showMessage(body.detail || 'Search failed.', 'bad');
      return;
    }
    const clips = body.clips || [];
    renderBanners(body.errors);
    setClips(clips);
    rowsShown = 0;
    // Lanes for the cameras asked about, plus any that answered without being asked.
    for (const clip of clips) {
      if (!laneKeys.includes(String(clip.camera_key))) laneKeys.push(String(clip.camera_key));
    }
    if (current) updateNav();
    if (!clips.length) {
      const names = laneKeys.map(cameraName).join(', ');
      showMessage(`No recordings for ${names} between ${fromInput.value} and ${toInput.value}. Try a wider range.`);
      return;
    }
    renderTimeline(start, end, laneKeys);
    renderMoreRows();
    const count = `${clips.length} clip${clips.length === 1 ? '' : 's'}`;
    if (body.truncated) {
      const which = (body.truncated_cameras || []).map(cameraName).join(', ');
      showMessage(`${count} — ${which || 'a camera'} hit the search limit; narrow the time range to see everything.`);
    } else {
      showMessage(`${count}.`);
    }
  } catch (err) {
    results.innerHTML = '';
    showMessage('Could not reach the add-on. Is it running?', 'bad');
  } finally {
    searchButton.disabled = false;
  }
});

/* ── Keyboard ────────────────────────────────────────────────────────────────
 * Space play/pause · ←/→ seek 5 s · j/k earlier/later clip · f fullscreen.
 * Never while typing in a field, and Space/arrows are left alone when focus is on
 * the video itself (its native controls already handle them) or on a button/link
 * (Space activates it).
 * ─────────────────────────────────────────────────────────────────────────── */
function toggleFullscreen(video) {
  const doc = document;
  if (doc.fullscreenElement || doc.webkitFullscreenElement) {
    const exit = doc.exitFullscreen || doc.webkitExitFullscreen;
    if (exit) Promise.resolve(exit.call(doc)).catch(() => {});
    return;
  }
  if (video.requestFullscreen) {
    Promise.resolve(video.requestFullscreen()).catch(() => {});
  } else if (video.webkitRequestFullscreen) {
    video.webkitRequestFullscreen();
  } else if (video.webkitEnterFullscreen) {
    video.webkitEnterFullscreen();      // iOS Safari / WKWebView
  }
}

function handleKey(event) {
  if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return;
  const target = event.target;
  const inside = (sel) => target && target.closest && target.closest(sel);
  if (inside('input, select, textarea, [contenteditable=""], [contenteditable="true"]')) return;
  if (!current) return;
  const video = current.player.video;
  const onVideo = target === video;
  switch (event.key) {
    case ' ':
    case 'Spacebar':
      if (onVideo || inside('button, a')) return;
      if (video.paused) video.play().catch(() => {}); else video.pause();
      break;
    case 'ArrowLeft':
    case 'ArrowRight': {
      if (onVideo) return;
      const delta = event.key === 'ArrowLeft' ? -5 : 5;
      let t = (video.currentTime || 0) + delta;
      if (Number.isFinite(video.duration)) t = Math.min(t, video.duration);
      video.currentTime = Math.max(0, t);
      break;
    }
    case 'j':
    case 'J':
      step(-1);
      break;
    case 'k':
    case 'K':
      step(1);
      break;
    case 'f':
    case 'F':
      toggleFullscreen(video);
      break;
    default:
      return;
  }
  event.preventDefault();
}
document.addEventListener('keydown', handleKey);

/* ── Wiring ───────────────────────────────────────────────────────────────── */
for (const button of document.querySelectorAll('[data-preset]')) {
  button.addEventListener('click', () => applyPreset(button.dataset.preset));
}
prevButton.addEventListener('click', () => step(-1));
nextButton.addEventListener('click', () => step(1));
closeButton.addEventListener('click', closePlayer);
showMoreButton.addEventListener('click', renderMoreRows);
autoplayInput.checked = loadPref(STORE.autoplay, false) === true;
autoplayInput.addEventListener('change', () => savePref(STORE.autoplay, autoplayInput.checked));

(async function init() {
  applyPreset('1h');
  try {
    await loadChannels();
  } catch (err) {
    showMessage(err.message, 'bad');
  }
  loadHealth();
})();
